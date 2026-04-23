---
id: austin-boulder-project
capabilities:
  - http
name: Austin Boulder Project
description: Class schedules and bookings for the Austin Bouldering Project gym
color: "#1e3a2f"
website: "https://boulderingproject.portal.approach.app"
---

# Austin Boulder Project

Class schedules and booking for the [Austin Bouldering Project](https://austinboulderingproject.com) — Texas's premier bouldering and fitness gym with locations in Springdale and Westgate.

Built on the **Tilefive** platform (`approach.app`), authenticated via **AWS Cognito**.

## Setup

No credentials needed to view the schedule — `get_schedule` is fully public.

To book classes, run the `login` tool once. Credentials are resolved in
this order:

1. **Caller-supplied** — `run login '{"email":"...", "password":"..."}'`.
2. **Credential providers** — an installed `@provides(login_credentials)`
   skill matched on `.approach.app` (1Password, macOS Keychain, etc.).
3. **`NeedsCredentials`** — structured error when neither path resolves,
   telling the agent what domain and fields it needs.

On success, the skill runs AWS Cognito `USER_PASSWORD_AUTH` and stashes
`{email, password, idToken, refreshToken}` in the credential store.
Authed tools (`book_class`, `get_my_memberships`, etc.) read the IdToken
automatically from `params.auth` on subsequent calls.

## Locations

| Name | ID |
|---|---|
| Austin Springdale | `6` (default) |
| Austin Westgate | `5` |

## Activity IDs

| Activity | ID |
|---|---|
| Climbing Classes | `4` |
| Yoga | `5` |
| Fitness | `6` |

## Examples

```js
// Next 3 days of classes at Springdale (default)
run({ skill: "austin-boulder-project", tool: "get_schedule" })

// One specific day, yoga only, at Westgate
run({ skill: "austin-boulder-project", tool: "get_schedule", params: {
  date: "2026-03-18",
  days: 1,
  location_id: 5,
  activity_ids: "5"
}})

// Book a class (use id from get_schedule)
run({ skill: "austin-boulder-project", tool: "book_class", params: {
  booking_instance_id: 826115
}})
```

Returned `class` entities carry `startDate` / `endDate` in UTC plus a
`timezone` field (`"America/Chicago"`) so renderers can shift to local
time without re-deriving the gym's tz.

Capacity fields:
- `capacity` — max registrants (from `event.maxCustomers`)
- `customerCount` — currently reserved
- `spotsRemaining` = `capacity - customerCount`
- `isFull` — convenience flag when `spotsRemaining == 0`

## Memberships

`get_my_memberships` filters to `status="active"` by default. Pass
`include_expired: true` to see historical rows (cancelled annuals,
old prepaid memberships, etc.) — useful for "what have I bought
before?" queries.

`book_class` auto-picks the caller's first active membership when
`membership_id` isn't supplied. For multi-membership accounts, pass
the explicit id returned by `get_my_memberships`.

## Technical Notes

See `requirements.md` for full reverse-engineering notes on the Tilefive API.

Key discoveries:
- `Authorization` header on the widgets API is the namespace string (`boulderingproject`), not a JWT
- `httpx` with `http2=True` is required — CloudFront WAF uses JA4 TLS fingerprinting that blocks urllib/requests
- Cognito auth uses `IdToken` (not `AccessToken`) for portal API calls
- The widgets `/cal` response uses `customerCount` (not `ticketsRemaining`) for current fullness; capacity lives on `event.maxCustomers`
