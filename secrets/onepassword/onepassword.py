"""1Password — credential provider for login_credentials and api_key.

Wraps the `op` CLI (https://developer.1password.com/docs/cli/) to
expose vault items to the engine's `resolve_login_credentials` and
`resolve_api_key` matchmaking. Read-only in P1.

The first `op` call of a session surfaces a native desktop-app
biometric prompt; once the session is cached, subsequent calls are
silent. The agent never handles raw secret values — the `__secrets__`
envelope routes them directly into the engine's credential store,
which returns only the requested fields to the caller.
"""

from __future__ import annotations

import json
from typing import Any

from agentos import (
    api_key,
    connection,
    login_credentials,
    normalize_email,
    provides,
    returns,
    shell,
    skill_secret,
    url,
)

connection(
    "local",
    description="1Password vault via the `op` CLI. Requires desktop app + CLI integration.",
    client="api")


# ---------------------------------------------------------------------------
# `op` CLI wrapper
# ---------------------------------------------------------------------------


async def _op_json(*args: str) -> Any:
    """Run `op <args> --format json` and return the parsed JSON.

    Returns `None` when `op` exits non-zero — typical when the user
    isn't signed in, the item doesn't exist, or the vault is locked.
    """
    result = await shell.run("op", args=list(args) + ["--format", "json"])
    if result.get("exit_code") != 0:
        return None
    stdout = result.get("stdout", "") or ""
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def _host_matches_domain(host: str, domain: str) -> bool:
    """Match the cookie-style rule: suffix match on the dot-stripped domain."""
    cd = domain.lstrip(".").lower()
    host = host.lower()
    return host == cd or host.endswith("." + cd)


def _item_urls(item: dict) -> list[str]:
    urls: list[str] = []
    for u in item.get("urls") or []:
        href = u.get("href") if isinstance(u, dict) else None
        if href:
            urls.append(href)
    return urls


def _item_matches_domain(item: dict, domain: str) -> bool:
    for href in _item_urls(item):
        try:
            parts = url.parse(href)
        except Exception:
            continue
        host = getattr(parts, "host", "") or ""
        if host and _host_matches_domain(host, domain):
            return True
    return False


def _field_value(item: dict, purpose: str | None = None, label: str | None = None) -> str | None:
    """Pull a field value off an expanded `op item get` dict.

    1Password fields have a `purpose` (`USERNAME`, `PASSWORD`, `NOTES`)
    on canonical Login fields and a `label` on everything else.
    """
    for f in item.get("fields") or []:
        if not isinstance(f, dict):
            continue
        if purpose and f.get("purpose") == purpose:
            v = f.get("value")
            if v:
                return str(v)
        if label and (f.get("label") or "").lower() == label.lower():
            v = f.get("value")
            if v:
                return str(v)
    return None


async def _find_login_item(domain: str, account: str | None) -> dict | None:
    """Return the first Login item whose URLs match `domain`."""
    items = await _op_json("item", "list", "--categories", "Login")
    if not isinstance(items, list):
        return None
    matching = [i for i in items if _item_matches_domain(i, domain)]
    if not matching:
        return None
    if account:
        filtered = [i for i in matching if (i.get("title") or "").lower() == account.lower()]
        if filtered:
            matching = filtered
    # `op item list` returns summaries. Expand the first match to get fields.
    return await _op_json("item", "get", matching[0]["id"])


@returns({"provided": "boolean", "identifier": "string"})
@provides(login_credentials, description="Reads {email, password} from 1Password Login items matching a domain")
@connection("local")
async def get_credentials(
    *,
    domain: str,
    account: str | None = None,
    **params,
) -> dict[str, Any]:
    """Match a 1Password Login item for `domain` and return `{email, password}`.

    Args:
        domain: Canonical credential-store domain (e.g. `".approach.app"`).
        account: Optional item-title disambiguator when multiple Login
                 items have URLs under the same domain.
    """
    item = await _find_login_item(domain, account)
    if item is None:
        return {"provided": False}

    username = _field_value(item, purpose="USERNAME")
    password_val = _field_value(item, purpose="PASSWORD")
    if not username or not password_val:
        return {"provided": False}

    identifier = normalize_email(username) if "@" in username else username
    secret = skill_secret(
        domain=domain,
        identifier=identifier,
        item_type="login_credentials",
        value={"email": identifier, "password": password_val},
        source="onepassword",
        metadata={
            "masked": {"password": "••••••••"},
            "op_item_id": item.get("id"),
            "op_item_title": item.get("title"),
            "op_vault_id": (item.get("vault") or {}).get("id"),
        },
    )
    return {
        "__secrets__": [secret],
        "__result__": {"provided": True, "identifier": identifier},
    }


@returns({"provided": "boolean", "identifier": "string"})
@provides(api_key, description="Reads API keys from 1Password API Credential items by service name")
@connection("local")
async def get_api_key(
    *,
    service: str,
    account: str | None = None,
    **params,
) -> dict[str, Any]:
    """Match a 1Password API Credential item for `service` and return its value.

    Args:
        service: Service name used to filter API Credential items
                 (matched against item title, case-insensitive).
        account: Optional item-title disambiguator when multiple API
                 Credential items exist for the same service.
    """
    items = await _op_json("item", "list", "--categories", "API Credential")
    if not isinstance(items, list):
        return {"provided": False}
    matching = [i for i in items if service.lower() in (i.get("title") or "").lower()]
    if account:
        filtered = [i for i in matching if (i.get("title") or "").lower() == account.lower()]
        if filtered:
            matching = filtered
    if not matching:
        return {"provided": False}

    item = await _op_json("item", "get", matching[0]["id"])
    if not isinstance(item, dict):
        return {"provided": False}
    key_value = _field_value(item, label="credential") or _field_value(item, purpose="NOTES")
    if not key_value:
        return {"provided": False}

    identifier = (item.get("title") or service).strip()
    secret = skill_secret(
        domain=service,
        identifier=identifier,
        item_type="api_key",
        value={"key": key_value},
        source="onepassword",
        metadata={
            "masked": {"key": "••••" + key_value[-4:] if len(key_value) >= 4 else "••••"},
            "op_item_id": item.get("id"),
            "op_item_title": item.get("title"),
        },
    )
    return {
        "__secrets__": [secret],
        "__result__": {"provided": True, "identifier": identifier},
    }
