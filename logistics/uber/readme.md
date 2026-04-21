---
id: uber
capabilities:
  - http
name: Uber
description: "Ride history, trip details, Eats order history, and account info from Uber"
color: "#000000"
website: "https://uber.com"

connections:
  web:
    description: Uber rider account — requires cookies from a logged-in browser session
    base_url: https://riders.uber.com
    domain: uber.com
    auth:
      type: cookies
      domain: .uber.com
      account:
        check: check_session
    label: Uber Rider
    help_url: https://riders.uber.com
  eats:
    description: Uber Eats — requires cookies from a logged-in ubereats.com browser session
    base_url: https://www.ubereats.com
    domain: ubereats.com
    auth:
      type: cookies
      domain: .ubereats.com
      account:
        check: check_eats_session
    label: Uber Eats
    help_url: https://www.ubereats.com

product:
  name: Uber
  website: https://uber.com
  developer: Uber Technologies

test:
  # Cookie-auth ops — skip by default; wire per-session when cookies are present.
  check_session:
    skip: true
  check_eats_session:
    skip: true
  whoami:
    skip: true
  get_eats_profile:
    skip: true
  list_trips:
    skip: true
  get_trip:
    skip: true
  list_deliveries:
    skip: true
  get_delivery:
    skip: true
  search_stores:
    skip: true
  get_store:
    skip: true
  get_item_customizations:
    skip: true
  search_products:
    skip: true
  search_address:
    skip: true
  list_addresses:
    skip: true
  get_messages:
    skip: true
  list_nearby_stores:
    skip: true
  add_to_cart:
    skip: true
  get_cart:
    skip: true
  clear_cart:
    skip: true
  checkout:
    skip: true
  track_delivery:
    skip: true
---

# Uber

Ride history, trip details, receipts, and account info from Uber. Uses Uber's internal GraphQL API at `riders.uber.com/graphql` via browser session cookies. Uber Eats uses a separate RPC API at `ubereats.com/_p/api/`.

> **Before extending this skill**, read:
> 1. [Reverse Engineering overview](../../docs/reverse-engineering/overview.md) — methodology, tools, progression
> 2. [Transport & Anti-Bot](../../docs/reverse-engineering/1-transport/index.md) — TLS fingerprinting, WAF bypass, cookie domain filtering
> 3. [requirements.md](./requirements.md) — captured API shapes, endpoint inventory, auth headers
> 4. [Uber Eats E2E spec](../../../docs/specs/uber-eats-e2e.md) — the plan for what we're building

## Features

### Rides
- **`list_trips`** — Ride history with pagination. Returns trip ID, destination, fare, date, and map URL. Supports `profile_type` (PERSONAL/BUSINESS) filter and pagination via `next_page_token`. Max 50 per page.
- **`get_trip`** — Full trip details: driver info, pickup/dropoff addresses, fare breakdown, distance, duration, vehicle type, surge pricing, map URL, and rating.

### Account
- **`whoami`** — Full user profile: name, email, phone, rating, picture URL, Uber One membership, payment methods, and profiles (personal/business).
- **`check_session`** — Validate session cookies and return account identity.

## Setup

Requires an active Uber session in Brave (or another browser). The skill extracts session cookies from the browser's cookie database. No API keys needed.

1. Log in to [riders.uber.com](https://riders.uber.com) in Brave
2. Cookies are extracted automatically when you use the skill

## Transport

Cookie auth against `.uber.com`. The rides API uses a single GraphQL endpoint:

```
POST https://riders.uber.com/graphql
```

**IMPORTANT:** Always use `http.headers(waf="cf", accept="json", extra={...})` for all HTTP
requests in this skill. The engine sets zero default headers — without `http.headers()`, you
get no User-Agent, no sec-ch-*, no Sec-Fetch-* — and some Uber endpoints reject the request.
We are acting as Brave, so always send what Brave sends. See `docs/skills/sdk.md`.

Rides-specific headers (pass via `extra=`):
- `x-csrf-token: x` (literal string, not a real CSRF token)
- `x-uber-rv-session-type: desktop_session`

Three GraphQL operations:
- `CurrentUserRidersWeb` — user profile
- `Activities` — trip history with filtering/pagination
- `GetTrip` — full trip details with receipt

### Cookie domain filtering

Uber has cookies on multiple subdomains (`.uber.com`, `.riders.uber.com`, `.auth.uber.com`). The engine's RFC 6265 domain matching ensures only cookies matching `riders.uber.com` are sent. This prevents `csid` collisions from sibling subdomains that caused login redirects before domain filtering was implemented.

See [Transport & Anti-Bot docs](../../docs/reverse-engineering/1-transport/index.md#cookie-domain-filtering--rfc-6265) for details.

## Uber Eats (in progress)

Uber Eats uses a **completely different API** from rides. It's NOT GraphQL — it's an RPC-style API at `www.ubereats.com/_p/api/`.

### Discovery (2026-04-02)

Used `browse capture` (CDP network capture via `bin/browse-capture.py`) to navigate `ubereats.com/orders` in Brave and capture all API calls. Key findings:

**Uber Eats API endpoints** (all `POST https://www.ubereats.com/_p/api/`):

| Endpoint | Purpose | Request body |
|----------|---------|-------------|
| `getPastOrdersV1` | Order history | `{ "lastWorkflowUUID": "" }` (pagination) |
| `getOrderEntitiesV1` | Order details — items, driver, receipt | `{}` |
| `getActiveOrdersV1` | Live orders in progress | `{ "orderUuid": null, "timezone": "America/Chicago" }` |
| `getCartsViewForEaterUuidV1` | Current cart state | `{}` |
| `getSearchHomeV2` | Store browsing / search | `{ "dropPastOrders": true }` |
| `getDraftOrdersByEaterUuidV1` | Draft (unsent) orders | `{ "removeAdapters": true }` |
| `getUserV1` | User profile for Eats | `{ "shouldGetSubsMetadata": true }` |
| `getProfilesForUserV1` | User profiles | `{}` |
| `getInstructionForLocationV1` | Delivery instructions | `{ "location": { "latitude": ..., "longitude": ... } }` |
| `setRobotEventsV1` | Bot detection telemetry | `{ "action": "rendered", "payload": { "isBot": false } }` |

**Auth headers for Eats** (different from rides):

```
x-csrf-token: x
x-uber-session-id: <from uev2.id.session cookie>
x-uber-target-location-latitude: 30.271044
x-uber-target-location-longitude: -97.695755
x-uber-client-gitref: <client version hash>
x-uber-ciid: <client instance ID>
x-uber-request-id: <UUID per request>
Content-Type: application/json
```

**Cookie domain:** `.ubereats.com` (NOT `.uber.com` — different domain from rides)

**Real-time events:** `ramenphx/events/recv` and `ramendca/events/recv` — likely SSE or long-polling for live delivery tracking updates.

**Key difference from rides:** The `order_types: "EATS"` parameter on the rides GraphQL `Activities` query does NOT work — `EATS` is not a valid enum value in `RVWebCommonActivityOrderType`. Uber Eats order history must be fetched from the Eats-specific `getPastOrdersV1` endpoint.

### Eats operations (shipped)

Read: `check_eats_session`, `get_eats_profile`, `list_deliveries`, `get_delivery`, `get_messages`, `list_nearby_stores`, `search_stores`, `search_products`, `get_store`, `get_item_customizations`, `search_address`, `list_addresses`.

Write: `add_to_cart`, `get_cart`, `clear_cart`, `checkout`, `track_delivery`.

Use `agentos call read '{"skill":"uber"}'` or `load({skill:"uber"})` for the live tool manifest with full param schemas — it's generated from the `@returns` decorators, so it's always in sync with the code.

### Ordering flow (MANDATORY for any agent placing a real order)

Placing a pizza-to-the-pizza-place order once was hilarious. Twice would be
embarrassing. Follow this sequence every time, in this order:

1. **Find the store.** `search_stores({query})` or use a past order's
   `store.id` from `list_deliveries` / `get_delivery`.
2. **Get the menu.** `get_store({store_uuid})`. The returned `offers[]` items
   carry a hidden `_raw` field with the full catalog payload — `add_to_cart`
   requires it, so pass `offers` items through directly; don't reconstruct.
3. **Customizations (if any).** `get_item_customizations({store_uuid, item_uuid})`.
   The skill now auto-resolves section/subsection UUIDs when omitted.
4. **Pick an explicit delivery address.** `list_addresses()` → choose by
   **`source == "SAVED"` + `label == "HOME"`** first. If `SAVED` is empty
   (common — Uber treats pasted/searched addresses as SUGGESTED until the user
   explicitly saves them), prompt the user to pick from the SUGGESTED list.
   **Never auto-pick a SUGGESTED entry.** Uber mixes real addresses and POIs
   (restaurants, shops) into SUGGESTED; auto-picking got a pizza delivered to
   the pizza restaurant on 2026-04-20.
5. **Build the cart.** `add_to_cart({store_uuid, items, delivery_address_uuid})`.
   The skill creates a draft via `createDraftOrderV2` and then **pins the
   address via `updateDraftOrderV2`** — `createDraftOrderV2` silently ignores
   `deliveryAddress` in its own body and inherits whatever the account's
   "active target" is (the thing that bit us).
6. **Pre-checkout checklist — SHOW THE USER, then wait for explicit go.**
   Surface all of:
   - **Store**: name + address
   - **Items**: name, customizations, quantity, price each
   - **Delivery address**: full address **and deliveryNotes** (gate codes,
     apartment numbers — critical for actual delivery)
   - **Total** (from the checkout presentation) + fare breakdown
   - **ETA**

   Do not call `checkout()` without an explicit "place it" from the user.
7. **Place the order.** `checkout({draft_order_uuid})`. This actually spends
   money; it's irreversible within seconds.
8. **Track.** `track_delivery()` polls `getActiveOrdersV1`. Returns driver,
   eta, and polyline traces — but **only while the order is active**. Once
   delivered/closed, driver + vehicle info are gone from Uber's API (privacy).
   `get_delivery` on a completed order returns store + items + fare but no
   driver.

### `get_messages` is ephemeral

Driver chat via `getEaterMessagingContentV1` only returns content while the
order is active or very recently delivered. Once the delivery completes and
Uber closes the chat, **the server returns empty body/head** — no message
history is persisted to the eater side. If you need a durable record, capture
messages during `track_delivery` polling, not after.

Also: the skill currently returns `{body, head, messages[]}` on the order
shape rather than proper `conversation` + `message[]` entities. Known kludge.
Fix when a future order actually produces a non-empty chat we can shape against.

### Troubleshooting

**`rtapi.forbidden` on `getUserV1` / `code=3` on `getPastOrdersV1` / `401` on `getDraftOrdersByEaterUuidV1`** — session cookies are visitor-level, not user-level. `search_stores` / anonymous endpoints still work because they don't need a logged-in identity, which masks the failure. The auth-resolver reports `ok` either way because the transport auth works; only the per-endpoint logic rejects.

Two common causes:

1. **Brave cookie staleness.** Brave (and all Chromium browsers) buffer cookie writes and only flush to the on-disk SQLite DB periodically. `get-cookie.py` reads from disk, so after a fresh login the skill sees the *pre-login* cookie set — `uev2.id.session` / `uev2.ts.session` / `jwt-session` are stale, and Uber downgrades the session to visitor. **Fix:** trigger Brave to flush. Easiest: open any ubereats.com page in Brave and let it make at least one authenticated XHR (the `/ramen*/events/recv` or `getUserV1` round-trip will do it). CDP-based `browse-capture.py /orders` also works and is scriptable. Repeat the skill call and it will see the fresh cookies.
2. **You're actually logged out.** Check that `uev2.id.session_v2` and `sid` are in the extracted cookie set. If not, log in to [ubereats.com](https://www.ubereats.com) in Brave.

**`Multiple accounts. Specify account: Joe, default`** — the skill has two `account` entries in credentials (a legacy one and the current session). Pass `account: "default"` to `run()` to disambiguate. We should collapse these — tracked but not yet done.

**`invalid_uuid` / 404 on `getMenuItemV1`** — `get_item_customizations` needs both `section_uuid` and `subsection_uuid`. The skill now auto-fetches them from `getStoreV1` when omitted; if you see this error again, the item UUID itself is stale or wrong.

## Reverse Engineering Notes

### Tools used

- **`agentos browse request uber`** — authenticated HTTP request with full header visibility. Used to verify cookie auth and inspect response headers.
- **`agentos browse cookies uber`** — cookie inventory showing all `.uber.com` cookies with timestamps and provenance.
- **`agentos browse auth uber`** — auth resolution trace showing which provider won (brave-browser) and identity (agentos@contini.co).
- **`bin/browse-capture.py`** — CDP network capture. Connected to Brave via CDP, navigated to `ubereats.com/orders`, captured 90 requests including all `/_p/api/` calls with full headers and POST bodies.

### How to extend

**Step 1: Capture network traffic with CDP**

```bash
# Launch Brave with CDP
open -a "Brave Browser" --args --remote-debugging-port=9222 --remote-allow-origins="*"

# Capture network traffic for any Uber Eats page
python3 bin/browse-capture.py https://www.ubereats.com/store/costco/... --port 9222

# Look for /_p/api/ POST requests in the output
# Response bodies are captured automatically via CDP Network.getResponseBody
```

**Step 2: Extract full API surface from JS bundles**

Don't just capture what one page loads — extract ALL endpoint names from the client JS:

```bash
# Find the main bundle URL from browse-capture output
# Then grep for API endpoint patterns
curl -s "https://www.ubereats.com/_static/client-main-*.js" \
  | grep -oE 'get[A-Z][a-zA-Z]+V[0-9]+' | sort -u   # read endpoints
curl -s "https://www.ubereats.com/_static/client-main-*.js" \
  | grep -oE '[a-z]+[A-Z][a-zA-Z]+V[0-9]+' | sort -u | grep -v '^get'  # write endpoints
```

This revealed 32 endpoints (22 read, 10 write) that weren't visible from a single page capture. The pattern `{verb}{Entity}V{version}` is consistent across all Uber Eats endpoints.

**Step 3: Test individual endpoints**

Use `agentos browse request` or direct `curl` to test specific endpoints. The auth headers and cookie domain are documented in [requirements.md](./requirements.md).

See [Reverse Engineering overview](../../docs/reverse-engineering/overview.md) for the full methodology and [Browse Toolkit spec](../../../docs/specs/browse-toolkit.md) for tool documentation.

### CDP tips for testing Eats endpoints

**Making authenticated API calls via CDP:**
```python
import json, urllib.request, websocket

# Connect to Brave (must be running with --remote-debugging-port=9222)
tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
ws = websocket.create_connection(tabs[0]["webSocketDebuggerUrl"], timeout=15)

# IMPORTANT: Navigate to ubereats.com first — fetch with credentials: 'include'
# only sends cookies for same-origin requests
ws.send(json.dumps({"id": 1, "method": "Page.navigate",
    "params": {"url": "https://www.ubereats.com/"}}))
import time; time.sleep(5)  # wait for page load

# Call any /_p/api/ endpoint
js = """
(async () => {
    const r = await fetch('https://www.ubereats.com/_p/api/getPastOrdersV1', {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json', 'x-csrf-token': 'x'},
        body: JSON.stringify({"lastWorkflowUUID": ""})
    });
    return await r.text();
})()
"""
ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
    "params": {"expression": js, "awaitPromise": True, "returnByValue": True}}))

# Read response (skip any navigation events)
for _ in range(20):
    resp = json.loads(ws.recv())
    if resp.get("id") == 2:
        data = json.loads(resp["result"]["result"]["value"])
        break
```

**Key gotchas:**
- Use `websocket` module (installed), NOT `websockets` (not installed). Synchronous API, no asyncio.
- Brave's cookie DB is encrypted — can't extract cookies from SQLite directly. Use CDP `Network.getCookies` or the agentOS engine's auth resolver.
- The `x-csrf-token: x` header is required. Other Eats headers (`x-uber-session-id`, `x-uber-target-location-*`) are optional for basic reads — the browser sends them automatically via cookies.
- When reading CDP responses, check `resp.get("id")` to match your request — navigation and other events arrive on the same websocket.
