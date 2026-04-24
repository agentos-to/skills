"""Reverse-engineering toolkit — Python tools an agent calls when
reverse-engineering a live web service.

The skill is agent-facing utility: no auth, no provider contract, no
graph shape return. It reaches into arbitrary user-supplied URLs
(unlike most skills, which are per-service), so the `public`
connection below is connection-scaffolding only — `client.get(...)`
accepts absolute URLs and the engine handles the dispatch.

Phase 1 of the RE-skill roadmap project: skeleton + one placeholder
tool to prove the skill-authoring path works end-to-end. Phases 2–5
add `capture`, `replay`, `find_bundles`, `hook_fetch`,
`dump_storage`, `eval`, and the rich `inspect_page` body. See
`_roadmap/p2/browser-stack/reverse-engineering-skill.md`.
"""

import re as _re
from typing import Any

from agentos import (
    client,
    connection,
    returns,
)


# The RE skill navigates to arbitrary user-supplied URLs — every tool
# takes its own absolute `url`, so there's no sensible `base_url` to
# bake in. The connection exists to satisfy the SDK's "tools need a
# connection" contract; client.get(absolute_url) bypasses base_url.
connection(
    "public",
    description="Arbitrary outbound HTTP for reverse-engineering. No auth.",
    client="fetch",
)


_BUNDLE_SCRIPT_RE = _re.compile(
    r'<script[^>]+src="(?P<src>[^"]+\.js[^"]*)"',
    _re.IGNORECASE,
)
_TITLE_RE = _re.compile(
    r"<title[^>]*>(?P<title>.*?)</title>",
    _re.IGNORECASE | _re.DOTALL,
)


@returns({"url": "string", "final_url": "string", "status": "number",
          "title": "string", "bundles": "array", "content_length": "number"})
@connection("public")
async def inspect_page(url: str, **params) -> dict[str, Any]:
    """Fetch `url` and return basic page metadata.

    Minimal Phase-1 inspection: status, final URL (after redirects),
    `<title>`, declared JS bundle URLs, response body length. The
    rich version in Phase 5 will detect frameworks (React / Next /
    Vue), dump Apollo / Redux state, and extract GraphQL endpoints
    from captured traffic.

    `bundles` is a de-duplicated list of absolute bundle URLs —
    useful for chaining into `find_bundles` (Phase 2) without a
    second fetch.
    """
    if not url:
        raise ValueError("url is required")

    resp = await client.get(url)
    body = resp.get("body") or ""

    title_match = _TITLE_RE.search(body)
    title = title_match.group("title").strip() if title_match else ""
    # Collapse whitespace — real titles sometimes span newlines in the HTML.
    title = _re.sub(r"\s+", " ", title)

    seen: set[str] = set()
    bundles: list[str] = []
    for m in _BUNDLE_SCRIPT_RE.finditer(body):
        src = m.group("src").strip()
        if not src or src in seen:
            continue
        seen.add(src)
        bundles.append(_absolutize(src, resp.get("url") or url))

    return {
        "url": url,
        "final_url": resp.get("url") or url,
        "status": int(resp.get("status") or 0),
        "title": title,
        "bundles": bundles,
        "content_length": len(body),
    }


def _absolutize(src: str, base: str) -> str:
    """Turn a bundle `src` (may be relative, protocol-relative, or
    absolute) into an absolute URL rooted at `base`. Pure string work
    — no URL parsing library needed for the three forms SPAs emit."""
    if src.startswith(("http://", "https://")):
        return src
    if src.startswith("//"):
        scheme = base.split(":", 1)[0] if "://" in base else "https"
        return f"{scheme}:{src}"
    if src.startswith("/"):
        # Root-relative — strip base to origin.
        if "://" in base:
            scheme, rest = base.split("://", 1)
            host = rest.split("/", 1)[0]
            return f"{scheme}://{host}{src}"
        return src
    # Path-relative — rare for bundles, but handle it.
    if "://" in base:
        # Strip query/fragment, then strip trailing file segment.
        clean = base.split("?", 1)[0].split("#", 1)[0]
        if "/" in clean.split("://", 1)[1]:
            clean = clean.rsplit("/", 1)[0]
        return f"{clean}/{src}"
    return src
