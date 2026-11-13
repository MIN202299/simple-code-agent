"""Web access: fetch a page as readable text, and search the web.

Only the standard library is used. HTML is reduced to text with light Markdown
(headings, links, lists, code) so it is cheap for the model to read.
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser

USER_AGENT = "Mozilla/5.0 (compatible; nori-cli)"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
FETCH_TIMEOUT = 20
MAX_DOWNLOAD_BYTES = 3_000_000
SEARCH_ENGINES = ("duckduckgo", "bing")

_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "head", "template", "iframe", "form"})
_BLOCK_TAGS = frozenset(
    "p div section article main header footer nav aside ul ol table tr blockquote "
    "h1 h2 h3 h4 h5 h6 pre br hr li dd dt figure figcaption".split()
)


class WebError(Exception):
    pass


class _TextExtractor(HTMLParser):
    """Collects the readable text of an HTML document as light Markdown."""

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._pre = 0
        self._in_title = False
        self._link: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("- ")
        elif tag == "pre":
            self._pre += 1
            self.parts.append("```\n")
        elif tag == "code" and not self._pre:
            self.parts.append("`")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self._link = urllib.parse.urljoin(self.base_url, href)
                self.parts.append("[")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in _SKIP_TAGS:
            self._skip = max(self._skip - 1, 0)
            return
        if self._skip:
            return
        if tag == "pre":
            self._pre = max(self._pre - 1, 0)
            self.parts.append("\n```\n")
        elif tag == "code" and not self._pre:
            self.parts.append("`")
        elif tag == "a" and self._link is not None:
            self.parts.append(f"]({self._link})")
            self._link = None
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data if self._pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        text = "".join(self.parts)
        lines = [line.rstrip() for line in text.split("\n")]
        out: list[str] = []
        in_code = False
        for line in lines:
            if line.strip() == "```":
                in_code = not in_code
            if not in_code:
                line = line.strip()
                if not line and (not out or not out[-1]):
                    continue  # collapse runs of blank lines
            out.append(line)
        text = "\n".join(out).strip()
        return re.sub(r"(?m)^(- .*)\n\n(?=- )", r"\1\n", text)  # keep list items together


def html_to_text(document: str, base_url: str = "") -> tuple[str, str]:
    """Returns (title, readable text)."""
    extractor = _TextExtractor(base_url)
    try:
        extractor.feed(document)
        extractor.close()
    except Exception:  # malformed markup: keep whatever was extracted
        pass
    return " ".join(extractor.title.split()), extractor.text()


def _download(url: str, user_agent: str = USER_AGENT, data: bytes | None = None) -> tuple[str, str, str]:
    """Returns (final url, content type, decoded body)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebError("only http and https URLs can be fetched")
    request = urllib.request.Request(
        url, data=data, headers={"User-Agent": user_agent, "Accept-Language": "en,zh;q=0.8"}
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            raw = response.read(MAX_DOWNLOAD_BYTES + 1)
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            final_url = response.geturl()
    except urllib.error.HTTPError as exc:
        raise WebError(f"HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise WebError(f"request failed: {getattr(exc, 'reason', exc)}") from exc
    try:
        body = raw[:MAX_DOWNLOAD_BYTES].decode(charset, errors="replace")
    except LookupError:
        body = raw[:MAX_DOWNLOAD_BYTES].decode("utf-8", errors="replace")
    return final_url, content_type, body


def fetch(url: str) -> str:
    """Fetch a URL and return its content as text the model can read."""
    final_url, content_type, body = _download(url)
    header = f"URL: {final_url}\n"
    if content_type in ("text/html", "application/xhtml+xml"):
        title, text = html_to_text(body, final_url)
        if title:
            header += f"Title: {title}\n"
        return f"{header}\n{text or '(the page has no readable text)'}"
    if content_type.startswith("text/") or content_type in (
        "application/json", "application/xml", "application/javascript", "application/x-yaml"
    ):  # fmt: skip
        return f"{header}\n{body}"
    raise WebError(f"cannot read content of type {content_type}")


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


def _clean(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def parse_duckduckgo(document: str) -> list[SearchResult]:
    results = []
    blocks = re.split(r'<div[^>]+class="[^"]*\bresult\b[^"]*"', document)[1:]
    for block in blocks:
        link = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url = html.unescape(link.group(1))
        redirect = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("uddg")
        if redirect:
            url = redirect[0]
        if url.startswith("//"):
            url = "https:" + url
        snippet = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', block, re.S)
        results.append(SearchResult(_clean(link.group(2)), url, _clean(snippet.group(1)) if snippet else ""))
    return results


def parse_bing(document: str) -> list[SearchResult]:
    results = []
    for block in re.split(r'<li class="b_algo"', document)[1:]:
        link = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        snippet = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        results.append(
            SearchResult(_clean(link.group(2)), html.unescape(link.group(1)),
                         _clean(snippet.group(1)) if snippet else "")
        )  # fmt: skip
    return results


def _search_with(engine: str, query: str) -> list[SearchResult]:
    quoted = urllib.parse.quote_plus(query)
    if engine == "bing":
        _, _, body = _download(f"https://www.bing.com/search?q={quoted}", BROWSER_USER_AGENT)
        return parse_bing(body)
    _, _, body = _download(
        "https://html.duckduckgo.com/html/", BROWSER_USER_AGENT, data=f"q={quoted}".encode()
    )
    return parse_duckduckgo(body)


def search(query: str, engine: str = "duckduckgo", limit: int = 8) -> list[SearchResult]:
    """Search the web. Falls back to the other engine if the first one fails or is empty."""
    order = [engine, *(other for other in SEARCH_ENGINES if other != engine)]
    errors = []
    for name in order:
        try:
            results = _search_with(name, query)
        except WebError as exc:
            errors.append(f"{name}: {exc}")
            continue
        if results:
            return results[:limit]
        errors.append(f"{name}: no results")
    raise WebError("; ".join(errors))
