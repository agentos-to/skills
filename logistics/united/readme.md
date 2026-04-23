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

Log in to [united.com](https://www.united.com) in Brave. Cookies are extracted automatically.

## Transport

Cookie auth against `.united.com`. Endpoint inventory lives in [requirements.md](./requirements.md).

## Reverse engineering notes

See [requirements.md](./requirements.md) for captured endpoints and auth details.

Session warming may be needed (United likely uses session-based CSRF tokens and progressive enhancement). TBD after CDP capture.
