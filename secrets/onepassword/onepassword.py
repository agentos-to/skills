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
    skill_error,
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


class _OpLocked(Exception):
    """Raised when `op` exits with a "not signed in / locked" error.

    Distinct from a no-match result — a locked vault and an empty
    match look identical to the caller otherwise, which is how
    silent "provided: false" bugs slip through.
    """


async def _op_json(*args: str) -> Any:
    """Run `op <args> --format json` and return the parsed JSON.

    Returns `None` on clean "no result" (e.g. `op item get` on an
    unknown id). Raises `_OpLocked` when the vault is locked / the
    session expired — callers surface that as a structured error.
    """
    result = await shell.run("op", args=list(args) + ["--format", "json"])
    exit_code = result.get("exit_code")
    if exit_code != 0:
        stderr = (result.get("stderr") or "").lower()
        if "not currently signed in" in stderr or "session expired" in stderr or "authorization prompt dismissed" in stderr:
            raise _OpLocked(result.get("stderr") or "op vault locked")
        return None
    stdout = result.get("stdout", "") or ""
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def _item_urls(item: dict) -> list[str]:
    urls: list[str] = []
    for u in item.get("urls") or []:
        href = u.get("href") if isinstance(u, dict) else None
        if href:
            urls.append(href)
    return urls


# Scoring thresholds — items below MIN_SCORE are treated as non-matches.
# URL match (same eTLD+1) is the strongest signal; title/tag matches act as
# fallback for items where the user never set a comprehensive URL field.
_URL_MATCH_SCORE = 100
_TITLE_EXACT_SCORE = 60
_TITLE_WORD_SCORE = 40
_TAG_MATCH_SCORE = 25
_MIN_SCORE = 40


def _score_item(item: dict, domain: str) -> int:
    """Score how well a 1Password item matches `domain`.

    Combines three signals:
      * URL host matches the domain's registrable root (eTLD+1)
      * Item title contains the domain's root label
      * Any item tag contains the domain's root label

    Returns a score; scores >= ``_MIN_SCORE`` are real matches.
    Tighter weighting would be more correct but also more brittle —
    the user's vault is hand-curated and signals are noisy.
    """
    # Root label: "amazon" from ".amazon.com", "approach" from ".approach.app"
    stripped = domain.strip().lstrip(".").lower()
    if not stripped:
        return 0
    target = url.registrable(stripped)
    root = target.split(".", 1)[0]

    score = 0

    # URL host matches — strongest signal
    for href in _item_urls(item):
        try:
            parts = url.parse(href)
        except Exception:
            continue
        host = getattr(parts, "host", "") or ""
        if host and url.same_site(host, target):
            score += _URL_MATCH_SCORE
            break  # one URL hit is enough; don't over-score multi-URL items

    # Title signal — catches items where the URL field is missing/stale
    title_lc = (item.get("title") or "").lower()
    if title_lc:
        title_words = title_lc.replace("-", " ").replace("_", " ").split()
        if title_lc == root:
            score += _TITLE_EXACT_SCORE
        elif root in title_words:
            score += _TITLE_WORD_SCORE

    # Tag signal — weakest
    for tag in (item.get("tags") or []):
        if root in tag.lower():
            score += _TAG_MATCH_SCORE
            break

    return score


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


def _candidate_summary(item: dict) -> dict:
    """Compact, secret-free summary of an item for multi-match disambiguation."""
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "username": (item.get("additional_information") or None),
        "vault": (item.get("vault") or {}).get("name"),
        "urls": _item_urls(item),
    }


async def _find_login_candidates(domain: str, account: str | None) -> list[dict]:
    """Return Login items matching `domain`, highest-score first.

    Filters to score >= ``_MIN_SCORE`` and ties cluster at the top
    score — i.e. a URL match always ranks above a title-only match,
    but two URL matches are both returned for the caller to
    disambiguate via ``account``.

    ``account`` narrows matches by either item title or the
    ``additional_information`` field (which 1Password populates with
    the username for Login items).
    """
    items = await _op_json("item", "list", "--categories", "Login")
    if not isinstance(items, list):
        return []
    scored = [(i, _score_item(i, domain)) for i in items]
    scored = [(i, s) for (i, s) in scored if s >= _MIN_SCORE]
    if not scored:
        return []

    if account:
        acc_lc = account.lower()
        narrowed = [
            (i, s) for (i, s) in scored
            if (i.get("title") or "").lower() == acc_lc
            or (i.get("additional_information") or "").lower() == acc_lc
        ]
        if narrowed:
            scored = narrowed

    scored.sort(key=lambda x: x[1], reverse=True)
    return [i for (i, _) in scored]


@returns("credential")
@provides(login_credentials, description="Reads {email, password} from 1Password Login items matching a domain")
@connection("local")
async def get_credentials(
    *,
    domain: str,
    account: str | None = None,
    **params,
) -> dict[str, Any]:
    """Match a 1Password Login item for `domain` and return `{email, password}`.

    Matching combines three signals: URL host (eTLD+1 match), item
    title contains the domain root, and tag contains the root.
    Items with weaker signals (title-only) are returned so vault
    entries with missing/stale URL fields still get found, but they
    rank below exact URL matches.

    Args:
        domain: Canonical credential-store domain (e.g. `".approach.app"`).
        account: Optional disambiguator — matches on item title OR
                 username. Used when multiple items tie on score.
    """
    try:
        candidates = await _find_login_candidates(domain, account)
    except _OpLocked as e:
        return skill_error(
            f"1Password vault is locked. Unlock the 1Password desktop app or run `op signin`, then retry. ({e})",
            code="OnePasswordLocked",
            help_url="https://developer.1password.com/docs/cli/get-started/#sign-in-to-your-account",
        )
    if not candidates:
        return {"provided": False}

    # Multi-match: if two or more items tie on the top score (e.g. two
    # Login items with URLs for the same site), return a structured
    # error listing candidates so the caller can retry with `account`.
    if len(candidates) > 1:
        top_score = _score_item(candidates[0], domain)
        tied = [c for c in candidates if _score_item(c, domain) == top_score]
        if len(tied) > 1:
            return skill_error(
                f"Multiple 1Password items match {domain!r}. "
                f"Retry with `account=` set to one of the listed usernames or titles.",
                code="MultipleMatches",
                domain=domain,
                candidates=[_candidate_summary(c) for c in tied],
            )

    # Top-scored match; expand to get field values (the list call
    # returns summaries only).
    try:
        item = await _op_json("item", "get", candidates[0]["id"])
    except _OpLocked as e:
        return skill_error(
            f"1Password vault locked mid-request. ({e})",
            code="OnePasswordLocked",
        )
    if not isinstance(item, dict):
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
    try:
        items = await _op_json("item", "list", "--categories", "API Credential")
    except _OpLocked as e:
        return skill_error(
            f"1Password vault is locked. Unlock the 1Password desktop app or run `op signin`, then retry. ({e})",
            code="OnePasswordLocked",
            help_url="https://developer.1password.com/docs/cli/get-started/#sign-in-to-your-account",
        )
    if not isinstance(items, list):
        return {"provided": False}
    matching = [i for i in items if service.lower() in (i.get("title") or "").lower()]
    if account:
        filtered = [i for i in matching if (i.get("title") or "").lower() == account.lower()]
        if filtered:
            matching = filtered
    if not matching:
        return {"provided": False}

    try:
        item = await _op_json("item", "get", matching[0]["id"])
    except _OpLocked as e:
        return skill_error(
            f"1Password vault locked mid-request. ({e})",
            code="OnePasswordLocked",
        )
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
