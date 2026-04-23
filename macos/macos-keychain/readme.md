---
id: macos-keychain
capabilities:
  - shell
name: macOS Keychain
description: >
  Credential provider backed by the macOS login Keychain. Exposes
  internet-password entries matching a domain via
  `@provides(login_credentials)` so skills' `login` tools can pull
  `{email, password}` without pasting anything.
color: "#D1D5DB"
website: "https://support.apple.com/guide/keychain-access"
---

# macOS Keychain

Wraps `/usr/bin/security` to expose macOS Keychain internet-password
entries as agentOS credentials. Read-only — this skill never writes to
the Keychain.

## How matchmaking works

When a skill's `login` tool calls
`credentials.retrieve(domain=".approach.app", required=["email", "password"])`,
the engine walks every installed skill that declares
`@provides(login_credentials)`. This skill's `get_credentials` tool
runs `security find-internet-password -s <host> -g` for each candidate
host under the domain, picks a match, and returns `{email, password}`
via the `__secrets__` envelope. The LLM never sees the raw password.

If no entry matches, the tool returns `{provided: false}` and the
engine moves on to other providers (1Password, browser password
stores, etc.).

## What we read from Keychain

Internet-password entries only (`security find-internet-password`).
Each carries `(server, account, password)` — `server` is the hostname,
`account` is the username (typically an email), `password` is the
secret. That maps exactly onto the
`login_credentials` contract: one server → one login.

Generic-password entries (`security find-generic-password`) are
deliberately excluded. They store things like API keys, encryption
keys, and per-app secrets; lumping them in with login_credentials
would give false positives to consumer skills asking for
`{email, password}`.

## Access prompt

The first `security -g` call against an entry the user hasn't
previously unlocked will surface a native "macOS wants to use your
confidential information stored in Keychain" prompt. Once approved,
subsequent reads are silent for the life of the process. That's the
one and only interaction users should see — everything else runs
without UI.
