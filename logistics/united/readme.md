---
id: united
capabilities:
- http
name: United Airlines
description: Flight search, reservations, boarding passes, travel history, and MileagePlus account access
color: '#002244'
website: https://www.united.com
product:
  name: United Airlines
  website: https://www.united.com
  developer: United Airlines, Inc.
test:
  check_session: {}
  get_profile: {}
  get_mileageplus: {}
  list_trips:
    params:
      upcoming_only: true
  search_flights:
    params:
      origin: AUS
      destination: SFO
      depart_date: '2026-04-28'
  store_session_cookies:
    skip: true  # requires runtime cookie params; not auto-testable
  select_flight:
    skip: true  # requires a live cart_id from search_flights; destructive (mints held cart)
  register_traveler:
    skip: true  # requires a cart_id from select_flight; commits PII to a held cart
  get_seatmap:
    skip: true  # requires a live cart_id; auto-test can't synthesize
  register_seats:
    skip: true  # destructive — commits a seat to a held cart
  render_seatmap:
    skip: true  # requires a live cart_id
---

# United Airlines

Flight search, booking, reservations, boarding passes, and MileagePlus account access via united.com session cookies.

> **Before extending this skill**, read:
> 1. [Reverse Engineering overview](../../../docs/src/content/docs/skills/reverse-engineering/overview.md)
> 2. [requirements.md](./requirements.md) — captured API shapes, endpoint inventory, auth details
> 3. The `reservation`, `flight`, `airline`, `airport`, and `pass` shape YAMLs under `docs/shapes/`

## Graph model

| Entity | Represents |
|--------|------------|
| **reservation** | A booking — 1+ passengers, 1+ trips, a PNR, payment, fare conditions |
| **trip** | A directed journey — e.g. outbound SFO→EWR of a round-trip |
| **flight** | A single segment (UA 1234 SFO→DEN) within a trip |
| **pass** | An issued ticket / boarding pass — one per passenger per flight |
| **airline** | United as an organization (UA / UAL / "UNITED") |
| **airport** | Origin/destination airports (IATA/ICAO codes, city, timezone) |
| **aircraft** | Equipment type (B789, A321 etc.) |
| **membership** | MileagePlus, Premier status, Club membership — keyed on MP#/CK# |
| **person** | The passenger (legal name, middle name etc.) |
| **account** | united.com login identity (email + customer key) |

Relationships:
- `reservation --at--> airline (United)`
- `reservation --passengers--> person[]`
- `reservation --trips--> trip[]` (one for outbound, one for return)
- `trip --legs--> flight[]` (multi-flight trips have multiple; nonstop has one)
- `reservation --tickets--> pass[]` (one per person per flight)
- `pass --holder--> person` + `pass --for--> flight`

## Features

Skeleton — nothing shipped yet. First milestone is `check_session` + `get_profile`.

## Setup

Two paths:

### 1. Brave cookie provider (zero-config, default)

Log in to [united.com](https://www.united.com) in Brave. The engine's
`brave-browser` provider extracts cookies from Brave's encrypted SQLite
DB. **Caveat:** Brave buffers cookie writes to disk and only flushes
periodically — immediately after a fresh login, the skill may see stale
cookies for up to ~5 minutes until Brave flushes. If `check_session`
returns SESSION_EXPIRED while you're clearly logged in in Brave, either
wait ~5 min or fully quit (Cmd+Q) and reopen Brave to force a flush.

### 2. `store_session_cookies` (bypasses Brave's stale DB)

For when you want the skill to use cookies independent of Brave's disk
state — e.g. you just logged in and don't want to wait. The agent grabs
live cookies (from CDP, the browser devtools Network tab, or any other
source) and passes them in:

```js
run({ skill: "united", tool: "store_session_cookies", params: {
  cookies: {
    AuthCookie: "…", Session: "…", User: "…",
    "PIM-SESSION-ID": "…", _ucid: "…", "1pc_session": "…",
    // …plus any Akamai bm_*, _abck, ak_bmsc cookies
  }
}});
```

The skill validates these against `/xapi/myunited/User/profile` (so we
know the cookies represent a real logged-in session, not an anonymous
visit) and persists them to the engine's credential store via
`__secrets__`. Because the engine resolves cookies by newest-timestamp
across all providers, these fresh cookies now beat Brave's stale DB on
every subsequent call.

## Transport

Cookie auth against `.united.com`. Endpoint inventory lives in [requirements.md](./requirements.md).

## Reverse engineering notes

See [requirements.md](./requirements.md) for captured endpoints and auth details.

Session warming may be needed (United likely uses session-based CSRF tokens and progressive enhancement). TBD after CDP capture.
