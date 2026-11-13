"""Background shell commands: start one, keep working, read its output later.

Used for things that do not finish on their own (a dev server, a file watcher)
or take long (a full build). Output is collected in memory, bounded in size.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_BUFFER_CHARS = 200_000
MAX_TASKS = 20


@dataclass
class ShellTask:
    id: str
    command: str
    process: asyncio.subprocess.Process
    started: float = field(default_factory=time.monotonic)
    output: str = ""
    read_to: int = 0  # how much of `output` the agent has already been given
    dropped: int = 0  # characters discarded from the front to bound memory
    reader: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.process.returncode is None

    @property
    def status(self) -> str:
        code = self.process.returncode
        return "running" if code is None else f"exited with code {code}"

    def append(self, text: str) -> None:
        self.output += text
        overflow = len(self.output) - MAX_BUFFER_CHARS
        if overflow > 0:
            self.output = self.output[overflow:]
            self.dropped += overflow
            self.read_to = max(self.read_to - overflow, 0)

    def take_new_output(self) -> str:
        new = self.output[self.read_to :]
        self.read_to = len(self.output)
        return new


def kill_process(process: asyncio.subprocess.Process) -> None:
    """Kill a process and, where possible, everything it started."""
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class ShellTasks:
    def __init__(self) -> None:
        self.tasks: dict[str, ShellTask] = {}
        self._counter = 0

    @property
    def running(self) -> list[ShellTask]:
        return [task for task in self.tasks.values() if task.running]

    async def start(self, command: str, cwd: Path) -> ShellTask:
        if len(self.running) >= MAX_TASKS:
            raise RuntimeError(f"too many background commands are running ({MAX_TASKS})")
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        self._counter += 1
        task = ShellTask(id=f"bg{self._counter}", command=command, process=process)
        task.reader = asyncio.ensure_future(self._read(task))
        self.tasks[task.id] = task
        return task

    @staticmethod
    async def _read(task: ShellTask) -> None:
        stream = task.process.stdout
        assert stream is not None
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            task.append(chunk.decode("utf-8", errors="replace"))
        await task.process.wait()

    def get(self, task_id: str) -> ShellTask | None:
        return self.tasks.get(task_id.strip())

    async def kill(self, task: ShellTask) -> None:
        kill_process(task.process)
        if task.reader is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task.reader), timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    def kill_all(self) -> None:
        """Stop everything; called when the session ends."""
        for task in self.tasks.values():
            kill_process(task.process)
            if task.reader is not None and not task.reader.done():
                task.reader.cancel()
