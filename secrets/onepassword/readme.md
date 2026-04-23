---
id: onepassword
capabilities:
  - shell
name: 1Password
description: >
  Credential provider backed by the 1Password CLI (`op`). Exposes
  Login and API-Credential vault items via
  `@provides(login_credentials)` and `@provides(api_key)` so skills'
  `login` tools can pull `{email, password}` or API keys without the
  user pasting anything.
color: "#0572EC"
website: "https://1password.com/"
---

# 1Password

Wraps the [1Password CLI](https://developer.1password.com/docs/cli/)
(`op`) to expose vault items as agentOS credential sources. Read-only
in P1 — this skill never writes to vaults. A future project covers the
full vault-import scope (credit cards, bank accounts, identity docs as
graph entities with tokenization).

## Setup

Install and sign in:

```bash
brew install --cask 1password-cli
eval $(op signin)
```

1Password's CLI talks to the desktop app via a local socket; the first
`op` call of a session will surface a native biometric / password
prompt from the desktop app, and subsequent calls are silent for the
cached session lifetime.

## Matchmaking

When a skill's `login` tool calls
`credentials.retrieve(domain=".approach.app", required=["email", "password"])`,
the engine dispatches this skill's `get_credentials` tool with the
domain. The tool runs `op item list --categories Login --format json`,
filters by the item's `urls[]` matching the domain, and returns the
first hit via `__secrets__`.

For API keys (`@provides(api_key)`), the tool looks in the
**API Credential** item category on a service-name match.

## Item-category mapping

| Request | 1Password category | Field shape |
|---|---|---|
| `login_credentials` | Login | `{email/username, password}` |
| `api_key` | API Credential | `{value}` (plus optional `username`, `type`) |

Vault-wide search uses whichever vault the current `op` session is
pointed at. Multi-account / multi-vault setups can disambiguate by
passing `params.account` on the original tool call — the engine's
"Multiple accounts. Specify account." structured error surfaces when
more than one item matches.

## Scope

**In scope (P1):**
- Read Login items matching a domain.
- Read API Credential items matching a service name.
- Return via `__secrets__` so the LLM never sees raw secret values.

**Out of scope (later):**
- Writes (create / update / delete items).
- Full vault import with credit cards, bank accounts, identity docs as
  graph entities. See
  [`_specs/skills/1password-integration.md`](../../../_specs/skills/1password-integration.md)
  for that scope.
