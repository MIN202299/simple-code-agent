"""Conversation compaction: replace old history with a summary to free context.

The model can only see `context_window` tokens. When a conversation approaches
that, the older part is summarised by the model itself and replaced with the
summary, keeping the most recent items verbatim.
"""

from __future__ import annotations

import json
from typing import Any

from agents import Agent, Model, Runner

DEFAULT_CONTEXT_WINDOW = 128_000
COMPACT_AT = 0.8  # auto-compact when the context reaches this share of the window
KEEP_RECENT_SHARE = 0.2  # share of the window kept verbatim after auto-compaction
KEEP_RECENT_MAX = 20_000
MAX_TOOL_OUTPUT_CHARS = 2_000
MAX_TOOL_ARGS_CHARS = 600

SUMMARY_PREFIX = (
    "[The earlier part of this conversation was compacted to save context. "
    "Summary of what happened so far:]\n\n"
)

SUMMARY_INSTRUCTIONS = """\
You are summarising a conversation between a user and a coding agent so that the \
agent can continue the work with only your summary instead of the full history. \
Whatever you leave out is lost, so be precise and concrete.

Write the summary in the language the user writes in, using these sections:

1. Goal: what the user asked for, including constraints and preferences they stated. \
Quote the most recent request verbatim.
2. State of the work: what has been done, with exact file paths, function names, \
commands that were run and their relevant results.
3. Key findings: facts about the codebase, errors encountered and how they were resolved, \
decisions made and why.
4. Remaining work: what is still to be done, in order, and the immediate next step.

Do not address the user, do not ask questions, and do not call tools. Output only the summary."""


def _text_of(content: Any) -> str:
    """Flatten message content, which is a string or a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "input_image":
                    parts.append("[image]")
                    continue
                parts.append(str(part.get("text") or part.get("refusal") or ""))
            else:
                parts.append(str(getattr(part, "text", "") or ""))
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def _get(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


IMAGE_TOKENS = 1_500  # rough cost of one attached image


def _without_image_data(value: Any) -> Any:
    """Replace inline image data, which is huge as text but cheap as tokens."""
    if isinstance(value, dict):
        if value.get("type") == "input_image":
            return {"type": "input_image", "image_url": "x" * IMAGE_TOKENS * 4}
        return {key: _without_image_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_image_data(item) for item in value]
    return value


def estimate_tokens(items: list[Any]) -> int:
    """Rough token count: ~4 ASCII characters or ~1 non-ASCII character per token."""
    text = json.dumps(_without_image_data(items), ensure_ascii=False, default=str)
    wide = sum(1 for ch in text if ord(ch) > 0x2E7F)
    return (len(text) - wide) // 4 + wide


def _safe_cuts(items: list[Any]) -> list[bool]:
    """For each index i, whether `items[:i]` / `items[i:]` is a valid place to split.

    A split must not separate a tool call from its result, nor a reasoning item
    from the item it belongs to.
    """
    output_at = {
        _get(item, "call_id"): index
        for index, item in enumerate(items)
        if _get(item, "type") == "function_call_output"
    }
    safe = []
    open_until = -1  # index of the latest result still owed to a call before i
    for index in range(len(items) + 1):
        previous = items[index - 1] if index else None
        if previous is not None and _get(previous, "type") == "function_call":
            open_until = max(open_until, output_at.get(_get(previous, "call_id"), len(items)))
        after_reasoning = previous is not None and _get(previous, "type") == "reasoning"
        safe.append(open_until < index and not after_reasoning)
    return safe


def find_cut(items: list[Any], keep_tokens: int) -> int:
    """Index splitting `items` into a part to summarise and a recent part to keep.

    Keeps roughly the last `keep_tokens` tokens, moving the split later if
    needed to land on a safe boundary. Returns 0 if nothing can be summarised.
    """
    start = len(items)
    total = 0
    for index in range(len(items) - 1, -1, -1):
        total += estimate_tokens([items[index]])
        if total > keep_tokens:
            break
        start = index
    safe = _safe_cuts(items)
    for index in range(start, len(items) + 1):
        if safe[index]:
            return index
    return 0


def transcript_text(items: list[Any], max_chars: int) -> str:
    """Render history as plain text, which every provider accepts as input."""
    lines = []
    for item in items:
        kind = _get(item, "type")
        if kind == "function_call":
            arguments = str(_get(item, "arguments") or "")[:MAX_TOOL_ARGS_CHARS]
            lines.append(f"[Agent called tool {_get(item, 'name')}: {arguments}]")
        elif kind == "function_call_output":
            output = _text_of(_get(item, "output"))
            if len(output) > MAX_TOOL_OUTPUT_CHARS:
                output = output[:MAX_TOOL_OUTPUT_CHARS] + " …[truncated]"
            lines.append(f"[Tool result: {output}]")
        elif kind in (None, "message"):
            text = _text_of(_get(item, "content")).strip()
            if text:
                speaker = {"user": "User", "assistant": "Agent"}.get(_get(item, "role"), "System")
                lines.append(f"{speaker}: {text}")
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        text = "[The beginning of the transcript was omitted.]\n\n" + text[-max_chars:]
    return text


def summary_item(summary: str) -> dict[str, Any]:
    return {"role": "user", "content": SUMMARY_PREFIX + summary.strip()}


async def summarise(
    model: str | Model | None,
    items: list[Any],
    context_window: int,
    instructions: str = "",
) -> tuple[str, int, int]:
    """Ask the model for a summary. Returns (summary, input tokens, output tokens)."""
    transcript = transcript_text(items, max_chars=context_window * 2)
    prompt = f"<transcript>\n{transcript}\n</transcript>\n\nSummarise the conversation above."
    if instructions:
        prompt += f"\nAdditional instructions from the user: {instructions}"
    agent = Agent(name="Summarizer", instructions=SUMMARY_INSTRUCTIONS, model=model)
    result = await Runner.run(agent, prompt, max_turns=1)
    usage = result.context_wrapper.usage
    return str(result.final_output or "").strip(), usage.input_tokens, usage.output_tokens
