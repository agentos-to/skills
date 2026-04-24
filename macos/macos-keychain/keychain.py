"""macOS Keychain — credential provider for login_credentials and password.

Wraps `/usr/bin/security find-internet-password` to expose the login
Keychain's internet-password entries as agentOS credential sources.
Read-only; nothing in this skill mutates the Keychain.

The engine's `resolve_login_credentials` pipeline matchmakes
`@provides(login_credentials)` tools at dispatch time — this skill's
`get_credentials` tool is invoked with a target domain, returns either
`{provided: true, identifier: <email>}` + a `__secrets__` envelope, or
`{provided: false}` when no entry matches.
"""

from __future__ import annotations

import re
from typing import Any

from agentos import (
    connection,
    login_credentials,
    normalize_email,
    password,
    provides,
    returns,
    shell,
    skill_secret,
)

connection(
    "local",
    description="macOS login Keychain via the `security` CLI. No network.",
    client="api")


# ---------------------------------------------------------------------------
# `security` output parser
# ---------------------------------------------------------------------------
#
# `security find-internet-password -s <host> -g` writes the attribute
# block to stdout and the decrypted password to stderr (because the
# tool historically wrote it to the TTY via /dev/tty). The attribute
# block looks like:
#
#     keychain: "/Users/joe/Library/Keychains/login.keychain-db"
#     version: 512
#     class: "inet"
#     attributes:
#         0x00000007 <blob>="boulderingproject.portal.approach.app"
#         "acct"<blob>="joe@example.com"
#         "atyp"<blob>=<NULL>
#         "cdat"<timedate>=0x32...
#         ...
#         "srvr"<blob>="boulderingproject.portal.approach.app"
#
# The password comes back on stderr as:  password: "the-password"

_ATTR_RE = re.compile(r'"(\w+)"<[^>]+>=(?:"([^"]*)"|<NULL>|(0x[0-9A-F]+))')
_PWD_RE = re.compile(r'password:\s*(?:"([^"]*)"|0x[0-9A-F]+\s+"([^"]*)")')


def _parse_attributes(stdout: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for m in _ATTR_RE.finditer(stdout):
        key = m.group(1)
        string_val = m.group(2)
        if string_val is not None:
            attrs[key] = string_val
    return attrs


def _parse_password(stderr: str) -> str | None:
    for m in _PWD_RE.finditer(stderr):
        if m.group(1) is not None:
            return m.group(1)
        if m.group(2) is not None:
            return m.group(2)
    return None


def _candidate_hosts(domain: str) -> list[str]:
    """Candidate hostnames to query Keychain for given a domain.

    Keychain's `-s` flag matches an exact server string. An agent asking
    about `.approach.app` typically wants credentials for whatever
    portal-hostname lives under that domain, so we try the domain with
    its leading dot stripped plus a handful of common prefixes
    (`www`, `portal`, `login`, `account`).
    """
    host = domain.lstrip(".")
    seen: list[str] = []

    def _push(value: str) -> None:
        if value and value not in seen:
            seen.append(value)

    _push(host)
    for prefix in ("portal", "login", "www", "account", "accounts"):
        _push(f"{prefix}.{host}")
    return seen


async def _find_entry(host: str, account: str | None) -> tuple[dict[str, str], str] | None:
    """Return `(attrs, password)` for a matching internet-password entry, or None."""
    args = ["find-internet-password", "-s", host, "-g"]
    if account:
        args.extend(["-a", account])
    result = await shell.run("/usr/bin/security", args=args)
    if result.get("exit_code") != 0:
        return None
    attrs = _parse_attributes(result.get("stdout", "") or "")
    pwd = _parse_password(result.get("stderr", "") or "")
    if not pwd or not attrs.get("acct"):
        return None
    return attrs, pwd


@returns("credential")
@provides(login_credentials, description="Reads {email, password} pairs from the macOS login Keychain")
@connection("local")
async def get_credentials(
    *,
    domain: str,
    account: str | None = None,
    **params,
) -> dict[str, Any]:
    """Match a login Keychain entry for `domain` and return `{email, password}`.

    Args:
        domain: Canonical credential-store domain (e.g. `".approach.app"`).
        account: Optional identifier to disambiguate when multiple entries exist.
    """
    for host in _candidate_hosts(domain):
        hit = await _find_entry(host, account)
        if not hit:
            continue
        attrs, pwd = hit
        email = normalize_email(attrs["acct"]) if "@" in attrs["acct"] else attrs["acct"]
        secret = skill_secret(
            domain=domain,
            identifier=email,
            item_type="login_credentials",
            value={"email": email, "password": pwd},
            source="macos-keychain",
            metadata={
                "masked": {"password": "••••••••"},
                "keychain_server": attrs.get("srvr", host),
            },
        )
        return {
            "__secrets__": [secret],
            "__result__": {"provided": True, "identifier": email},
        }
    return {"provided": False}


@returns({"provided": "boolean", "identifier": "string"})
@provides(password, description="Reads a password from macOS Keychain given domain + identifier")
@connection("local")
async def get_password(
    *,
    domain: str,
    account: str,
    **params,
) -> dict[str, Any]:
    """Return just the password for a known `(domain, account)` pair.

    Used when a caller already knows the account identifier (typical for
    re-login flows) and just needs the password refreshed. Same
    `__secrets__` envelope as `get_credentials` but without the email.
    """
    for host in _candidate_hosts(domain):
        hit = await _find_entry(host, account)
        if not hit:
            continue
        _attrs, pwd = hit
        secret = skill_secret(
            domain=domain,
            identifier=account,
            item_type="password",
            value={"password": pwd},
            source="macos-keychain",
            metadata={"masked": {"password": "••••••••"}},
        )
        return {
            "__secrets__": [secret],
            "__result__": {"provided": True, "identifier": account},
        }
    return {"provided": False}
