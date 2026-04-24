"""Brave Browser — CDP access provider.

Implements the `cdp_access` capability: *"give me a CDP WebSocket URL
to a debug-attachable Brave browser."* Tiny surface — one tool,
`cdp_connect`. The caller (typically the `browser-control` skill's
`browser_session` provider) opens its own WebSocket from the returned
URL and drives CDP from there. This module knows Brave-specific
things (profile paths, DevToolsActivePort, launch flags, debug port);
it knows nothing about what callers do with the session.

Architecture: see `_roadmap/p2/browser-control-skill.md`. Three
layers — RE / scraping / MFA skills consume `browser_session`;
`browser-control` provides `browser_session` and consumes
`cdp_access`; this module provides `cdp_access`. Zero cross-skill
imports; engine matchmakes every boundary.

Phase 1 ships **attach mode only**. Brave must be running with
`--remote-debugging-port=<port>`. If it isn't, we return a
structured `NeedsDebugBrowser` error that tells the agent exactly
how to fix it. Launch mode (spawn a fresh debug-attachable instance
with an isolated profile) is a Phase-2 follow-up once we have a
clear SDK story for detached background processes.
"""

import json
import os
from typing import Any

from agentos import (
    cdp_access,
    client,
    connection,
    provides,
    returns,
    skill_error,
    timeout,
)


# ──────────────────────────────────────────────────────────────────────
# Brave-specific constants
# ──────────────────────────────────────────────────────────────────────

_BRAVE_BASE = os.path.expanduser(
    "~/Library/Application Support/BraveSoftware/Brave-Browser"
)

# DevToolsActivePort is a two-line file Chromium writes into the user
# data directory when launched with --remote-debugging-port. Line 1 is
# the port (auto-assigned when the flag is given `0`); line 2 is an
# opaque token that's part of the WebSocket path for initial handshake
# (we don't use it — /json/version gives us the canonical URL).
_DEVTOOLS_ACTIVE_PORT_FILE = os.path.join(_BRAVE_BASE, "DevToolsActivePort")


# ──────────────────────────────────────────────────────────────────────
# Connection — public, no auth. We only hit the local debug endpoint.
# ──────────────────────────────────────────────────────────────────────

connection(
    "cdp",
    description="Brave's local CDP HTTP endpoint — /json/version etc. "
                "No auth (loopback-only by Chromium's design).",
    client="fetch",
)


# ──────────────────────────────────────────────────────────────────────
# Port discovery
# ──────────────────────────────────────────────────────────────────────

def _read_devtools_active_port() -> int | None:
    """Read the auto-assigned port Brave wrote to DevToolsActivePort.

    File exists iff Brave launched with `--remote-debugging-port`.
    Missing file = Brave not in debug mode (the common case — users
    don't launch with debug flags by default).
    """
    if not os.path.exists(_DEVTOOLS_ACTIVE_PORT_FILE):
        return None
    try:
        with open(_DEVTOOLS_ACTIVE_PORT_FILE, "r") as f:
            first_line = f.readline().strip()
        return int(first_line) if first_line else None
    except (OSError, ValueError):
        return None


async def _fetch_version(port: int) -> dict[str, Any] | None:
    """GET /json/version on the debug port. Returns Chromium's version
    info plus — critically — `webSocketDebuggerUrl`, the canonical WS
    endpoint for the browser-level target (not a page target).

    Returns None on any failure — the caller turns that into a
    structured error with the right code (NeedsDebugBrowser vs
    CDPConnectFailed).
    """
    try:
        resp = await client.get(f"http://127.0.0.1:{port}/json/version",
                                 timeout=3.0)
    except Exception:
        return None
    if resp.get("status") != 200:
        return None
    body = resp.get("json") or {}
    return body if isinstance(body, dict) else None


async def _fetch_targets(port: int) -> list[dict[str, Any]]:
    """GET /json to list all CDP targets (pages, workers, iframes).

    Each target has {id, type, title, url, webSocketDebuggerUrl}.
    Callers that want a specific tab (by URL or title) pick from this
    list. Browser-level control uses the /json/version endpoint
    instead, which is a different, durable target.
    """
    try:
        resp = await client.get(f"http://127.0.0.1:{port}/json",
                                 timeout=3.0)
    except Exception:
        return []
    if resp.get("status") != 200:
        return []
    body = resp.get("json")
    return body if isinstance(body, list) else []


# ──────────────────────────────────────────────────────────────────────
# The tool
# ──────────────────────────────────────────────────────────────────────

@returns({
    "ws_url": "string",
    "target_id": "string",
    "browser_version": "string",
    "attached_to": "string",
    "tabs": "array",
})
@provides(
    cdp_access,
    description="Chrome DevTools Protocol access to a running Brave "
                "browser. Requires Brave launched with "
                "--remote-debugging-port. Returns a WebSocket URL for "
                "the browser-level CDP target plus the current tabs "
                "list so callers can pick a page target if needed.",
)
@connection("cdp")
@timeout(10)
async def cdp_connect(
    *,
    mode: str = "attach",
    port: int | None = None,
    **params,
) -> dict[str, Any]:
    """Return a CDP WebSocket URL for a debug-attachable Brave.

    Args:
        mode: `"attach"` (the only mode supported in Phase 1). Finds
            a running Brave with a debug port. Launch mode (spawning
            a fresh instance) is a Phase-2 follow-up.
        port: Optional specific port to try. When `None`, we read
            `DevToolsActivePort` — the file Brave writes inside its
            user-data dir when launched with debug enabled.

    Returns a shape the `browser_session` provider (or any other
    caller) can use to open its own WebSocket:
        {
          ws_url:          browser-level CDP endpoint
          target_id:       browser target id (opaque)
          browser_version: "HeadlessChrome/..." or "Brave/..."
          attached_to:     informative string ("Brave on port 9222")
          tabs:            [{id, type, title, url, webSocketDebuggerUrl}]
        }

    Structured errors (surfaced via `skill_error`):
        - NeedsDebugBrowser: no debug port found. Message includes the
          exact relaunch command.
        - CDPConnectFailed: port found but /json/version failed
          (browser frozen, protocol mismatch).
        - UnsupportedMode: mode != "attach" (launch not yet supported).
    """
    if mode != "attach":
        return skill_error(
            f"Mode {mode!r} not yet supported. Phase 1 ships attach "
            f"mode only — Brave must be running with "
            f"--remote-debugging-port. Launch mode is queued.",
            code="UnsupportedMode",
            mode=mode,
            supported=["attach"],
        )

    # Prefer an explicit port; otherwise read DevToolsActivePort.
    resolved_port = port if port is not None else _read_devtools_active_port()
    if resolved_port is None:
        return skill_error(
            "Brave is not running with --remote-debugging-port. "
            "Quit Brave and relaunch it with the debug flag:\n\n"
            "    /Applications/Brave\\ Browser.app/Contents/MacOS/Brave\\ Browser "
            "--remote-debugging-port=9222\n\n"
            "Then retry this call. Alternatively, pass `port=<N>` if "
            "you have a debug instance running on a known port.",
            code="NeedsDebugBrowser",
            help_command=(
                "/Applications/Brave\\ Browser.app/Contents/MacOS/Brave\\ Browser "
                "--remote-debugging-port=9222"
            ),
            devtools_file_checked=_DEVTOOLS_ACTIVE_PORT_FILE,
        )

    version = await _fetch_version(resolved_port)
    if version is None:
        return skill_error(
            f"Found Brave debug port {resolved_port} but /json/version "
            f"failed. The browser may be frozen, or the protocol may "
            f"have drifted. Try restarting Brave.",
            code="CDPConnectFailed",
            port=resolved_port,
        )

    ws_url = version.get("webSocketDebuggerUrl") or ""
    if not ws_url:
        return skill_error(
            f"Brave responded on port {resolved_port} but omitted "
            f"webSocketDebuggerUrl. Unexpected — likely a Chromium "
            f"version incompatibility.",
            code="CDPConnectFailed",
            port=resolved_port,
            version=version,
        )

    # Browser-level target ID is embedded in the WS path:
    # ws://127.0.0.1:PORT/devtools/browser/<uuid>
    target_id = ws_url.rsplit("/", 1)[-1]
    browser_version = version.get("Browser") or "unknown"

    tabs = await _fetch_targets(resolved_port)

    return {
        "ws_url": ws_url,
        "target_id": target_id,
        "browser_version": browser_version,
        "attached_to": f"Brave on port {resolved_port}",
        "tabs": tabs,
    }
