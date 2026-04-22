"""Austin Boulder Project — Tilefive Portal API.

Two connections:
  - public: https://widgets.api.prod.tilefive.com (unauth — schedule, locations)
  - portal: https://portal.api.prod.tilefive.com  (authed — bookings, memberships)

Authed calls need a session token. The token is minted via AWS Cognito
USER_PASSWORD_AUTH and sent as the Authorization header. Password lives
in the credential store as "email:password"; tokens are minted fresh
per invocation (cheap; Cognito responds in ~300ms). Refresh flow is
internal — no SDK primitive needed.
"""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agentos import http, returns, test

AUSTIN_TZ_NAME = "America/Chicago"
AUSTIN_TZ = ZoneInfo(AUSTIN_TZ_NAME)

NAMESPACE = "boulderingproject"
PORTAL_ORIGIN = "https://boulderingproject.portal.approach.app"
PORTAL_API = "https://portal.api.prod.tilefive.com"
WIDGETS_API = "https://widgets.api.prod.tilefive.com"
COGNITO_ENDPOINT = "https://cognito-idp.us-east-1.amazonaws.com/"

AUSTIN_SPRINGDALE_ID = 6
AUSTIN_WESTGATE_ID = 5

# ---------------------------------------------------------------------------
# Config discovery — widgets API key, Cognito pool/client are in the bundle
# ---------------------------------------------------------------------------

_RE_BUNDLE_URL  = re.compile(r'src="/assets/(app-[A-Za-z0-9_-]+\.js)"')
_RE_WIDGETS_KEY = re.compile(r'widgetsApiKey:\{"us-east-1":"([^"]{30,})"')
_RE_POOL_ID     = re.compile(r'userPoolId:"(us-east-1_[A-Za-z0-9]+)"')
_RE_CLIENT_ID   = re.compile(r'userPoolClientId:"([A-Za-z0-9]{20,60})"')

_config_cache: dict | None = None


async def _discover_config(force: bool = False) -> dict:
    """Extract widgetsApiKey + Cognito pool/client from the portal bundle.

    Same values every visitor sees; we re-read at runtime so the skill
    survives Tilefive redeploys without shipping a new skill version.
    """
    global _config_cache
    if _config_cache and not force:
        return _config_cache

    html = await http.get(PORTAL_ORIGIN)
    if html["status"] >= 400:
        raise RuntimeError(f"portal HTML fetch failed: {html['status']}")
    m = _RE_BUNDLE_URL.search(html["body"] or "")
    if not m:
        raise RuntimeError("portal HTML has no app-*.js bundle reference")
    bundle_url = f"{PORTAL_ORIGIN}/assets/{m.group(1)}"

    bundle = await http.get(bundle_url, headers={
        "Referer": f"{PORTAL_ORIGIN}/",
        "Origin": PORTAL_ORIGIN,
    })
    text = bundle["body"] or ""
    km = _RE_WIDGETS_KEY.search(text)
    pm = _RE_POOL_ID.search(text)
    cm = _RE_CLIENT_ID.search(text)
    if not (km and pm and cm):
        missing = [n for n, v in [("widgetsApiKey", km), ("poolId", pm), ("clientId", cm)] if not v]
        raise RuntimeError(f"bundle missing {missing} — regex patterns may need updating")

    _config_cache = {
        "widgetsApiKey": km.group(1),
        "cognitoPoolId": pm.group(1),
        "cognitoClientId": cm.group(1),
    }
    return _config_cache


# ---------------------------------------------------------------------------
# Authed token — USER_PASSWORD_AUTH gets IdToken, used as raw Authorization
# ---------------------------------------------------------------------------

async def _mint_id_token(credentials: str) -> str:
    """Credential blob is 'email:password'. Returns a Cognito IdToken.

    IdToken, not AccessToken — Tilefive's portal API checks IdToken directly.
    """
    if not credentials or ":" not in credentials:
        raise ValueError(
            "ABP credentials must be 'email:password'. Add via "
            "`accountos call accounts '{\"action\":\"add_credential\",\"skill\":\"austin-boulder-project\",\"value\":\"EMAIL:PASSWORD\"}'`"
        )
    email, password = credentials.split(":", 1)
    cfg = await _discover_config()
    resp = await http.post(
        COGNITO_ENDPOINT,
        json={
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": cfg["cognitoClientId"],
            "AuthParameters": {"USERNAME": email.strip(), "PASSWORD": password.strip()},
        },
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
    )
    if resp["status"] >= 400:
        raise RuntimeError(f"Cognito login failed: {resp['status']} {(resp.get('body') or '')[:200]}")
    return resp["json"]["AuthenticationResult"]["IdToken"]


def _portal_headers(id_token: str) -> dict:
    return {
        "Authorization": id_token,
        "Origin": PORTAL_ORIGIN,
        "Referer": f"{PORTAL_ORIGIN}/",
    }


def _widgets_headers(widgets_key: str) -> dict:
    return {
        "X-Api-Key": widgets_key,
        "Authorization": NAMESPACE,   # namespace, not a JWT — API-Gateway tenant routing
        "Origin": PORTAL_ORIGIN,
        "Referer": f"{PORTAL_ORIGIN}/",
    }


async def _authed_get(params: dict, path: str, query: dict | None = None) -> dict:
    token = await _mint_id_token(params.get("auth", {}).get("key", ""))
    resp = await http.get(f"{PORTAL_API}{path}", headers=_portal_headers(token), params=query)
    if resp["status"] >= 400:
        raise RuntimeError(f"GET {path} -> {resp['status']}: {(resp.get('body') or '')[:200]}")
    return resp["json"]


async def _current_customer_id(params: dict, token: str) -> int:
    """Portal endpoint that returns the authenticated user profile."""
    resp = await http.get(f"{PORTAL_API}/customers", headers=_portal_headers(token))
    if resp["status"] >= 400:
        raise RuntimeError(f"GET /customers -> {resp['status']}")
    return int(resp["json"]["id"])


async def _active_membership_id(token: str) -> int | None:
    """Find the active membership to bill a class reservation against.

    Tilefive's booking endpoint needs an explicit `membershipId` — even if
    the user has only one active membership, omitting it yields a cryptic
    "Pass or Membership required" error. We pick the first active row;
    users with multiple actives will want a per-class pick UX eventually.
    """
    resp = await http.get(f"{PORTAL_API}/customers/memberships", headers=_portal_headers(token))
    if resp["status"] >= 400:
        return None
    for m in resp["json"] or []:
        if m.get("isActive") and (m.get("status") or "").lower() == "active":
            return int(m["id"])
    return None


# ---------------------------------------------------------------------------
# Entity helpers
# ---------------------------------------------------------------------------

def _austin_day_window_utc(date_str: str | None, days: int = 1) -> tuple[str, str]:
    if date_str:
        start_local_date = datetime.fromisoformat(date_str).date()
    else:
        start_local_date = datetime.now(AUSTIN_TZ).date()
    start_local = datetime.combine(start_local_date, datetime.min.time(), tzinfo=AUSTIN_TZ)
    end_local = start_local + timedelta(days=days)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc) - timedelta(milliseconds=1)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return iso(start_utc), iso(end_utc)


def _location_to_entity(loc: dict) -> dict:
    """Tilefive location → generic `place` shape."""
    return {
        "id": loc.get("id"),
        "name": loc.get("name") or loc.get("locationName") or f"ABP Location {loc.get('id')}",
        "street": loc.get("address1"),
        "city": loc.get("city"),
        "region": loc.get("state"),
        "postalCode": loc.get("postalCode") or loc.get("zipCode"),
        "countryCode": loc.get("countryCode") or "US",
        "latitude": loc.get("latitude"),
        "longitude": loc.get("longitude"),
        "phone": loc.get("phone"),
        "timezone": loc.get("timezone") or AUSTIN_TZ_NAME,
        "featureType": "poi",
    }


def _booking_to_entity(b: dict) -> dict:
    event = b.get("event", {})
    activities = event.get("activitys") or []
    activity_name = activities[0].get("name", "") if activities else ""
    spots = b.get("ticketsRemaining")
    capacity = event.get("maxCustomers")
    full = spots == 0
    desc = []
    if activity_name: desc.append(activity_name)
    if full: desc.append("FULL")
    elif spots is not None: desc.append(f"{spots}/{capacity} spots")
    return {
        "id": b["id"],
        "name": b["name"],
        "content": " — ".join(desc),
        "startDate": b.get("startDT"),
        "endDate": b.get("endDT"),
        "timezone": AUSTIN_TZ_NAME,
        "activityType": activity_name,
        "capacity": capacity,
        "spotsRemaining": spots,
        "isFull": full,
    }


# ---------------------------------------------------------------------------
# Operations — public connection (no credentials)
# ---------------------------------------------------------------------------

@returns("void")
async def public_authenticate(*, force: bool = False, **params) -> dict:
    """Force re-reading widgetsApiKey + Cognito config from the live bundle.

    Public because these values are embedded in the portal's own JS bundle.
    """
    return await _discover_config(force=bool(force))


@test
@returns("place[]")
async def get_locations(**params) -> list[dict]:
    """List all Bouldering Project locations as place entities.

    Austin has two — Springdale (id=6) and Westgate (id=5). Shape-
    typed so "what gyms are there?" works cross-skill.
    """
    cfg = await _discover_config()
    resp = await http.get(f"{WIDGETS_API}/locations", headers=_widgets_headers(cfg["widgetsApiKey"]))
    if resp["status"] >= 400:
        raise RuntimeError(f"/locations -> {resp['status']}")
    # Tilefive returns {"data": [...]} or raw [...]; handle both
    raw = resp["json"]
    rows = raw.get("data") if isinstance(raw, dict) else raw
    return [_location_to_entity(loc) for loc in (rows or [])]


@test
@returns("class[]")
async def get_schedule(
    location_id: int = AUSTIN_SPRINGDALE_ID,
    activity_ids: str | list | None = None,
    date: str | None = None,
    days: int = 3,
    **params,
) -> list[dict]:
    """Get the upcoming class schedule as entity-shaped dicts.

    Args:
        location_id:  6 = Austin Springdale (default), 5 = Austin Westgate
        activity_ids: "4,5,6" or [4,5,6] — Climbing(4), Yoga(5), Fitness(6)
        date:         YYYY-MM-DD, Austin-local; default: today in Austin
        days:         number of days from `date` (default 3)
    """
    if isinstance(activity_ids, str):
        ids = [int(x.strip()) for x in activity_ids.split(",") if x.strip()]
    elif isinstance(activity_ids, list):
        ids = [int(x) for x in activity_ids]
    else:
        ids = [4, 5, 6]

    start_dt, end_dt = _austin_day_window_utc(date, days=int(days))
    cfg = await _discover_config()
    resp = await http.get(
        f"{WIDGETS_API}/cal",
        headers=_widgets_headers(cfg["widgetsApiKey"]),
        params={
            "startDT": start_dt, "endDT": end_dt,
            "locationId": int(location_id),
            "activityId": ",".join(str(i) for i in ids),
            "page": 1, "pageSize": 50,
        },
    )
    if resp["status"] >= 400:
        raise RuntimeError(f"/cal -> {resp['status']}")
    return [_booking_to_entity(b) for b in resp["json"].get("bookings", [])]


# ---------------------------------------------------------------------------
# Operations — portal connection (credentials required)
# ---------------------------------------------------------------------------

@test.skip(reason="destructive — actually books a class")
@returns({"ok": "boolean", "message": "string"})
async def book_class(
    booking_instance_id: int,
    num_guests: int = 0,
    membership_id: int | None = None,
    **params,
) -> dict:
    """Book a class for the authenticated user.

    The booking is billed against a specific membership. If the caller
    doesn't pass `membership_id`, the skill looks up the user's first
    active membership. Explicit override supported for multi-membership
    users.
    """
    token = await _mint_id_token(params.get("auth", {}).get("key", ""))
    customer_id = await _current_customer_id(params, token)
    if membership_id is None:
        membership_id = await _active_membership_id(token)
        if membership_id is None:
            return {
                "ok": False,
                "message": "No active membership or pass — purchase one at "
                "https://boulderingproject.portal.approach.app/ to book classes.",
            }
    resp = await http.post(
        f"{PORTAL_API}/bookings/{int(booking_instance_id)}/customers",
        headers=_portal_headers(token),
        json={
            "customerId": customer_id,
            "numGuests": int(num_guests),
            "membershipId": int(membership_id),
        },
    )
    if resp["status"] >= 400:
        body = resp.get("body") or ""
        j = resp.get("json") or {}
        msg = j.get("message") if isinstance(j, dict) else None
        return {"ok": False, "message": msg or f"HTTP {resp['status']}: {body[:200]}"}
    j = resp.get("json") or {}
    return {
        "ok": True,
        "message": f"Booked. reservation_id={j.get('id')}",
        "reservation_id": j.get("id"),
        "booking_instance_id": j.get("bookingId"),
        "result": j,
    }


@test.skip(reason="destructive — cancels a real reservation")
@returns({"ok": "boolean", "message": "string"})
async def cancel_booking(booking_instance_id: int, reservation_id: int, **params) -> dict:
    """Cancel a class reservation (reservation_id comes from book_class result or get_my_bookings)."""
    token = await _mint_id_token(params.get("auth", {}).get("key", ""))
    resp = await http.delete(
        f"{PORTAL_API}/bookings/{int(booking_instance_id)}/reservations/{int(reservation_id)}",
        headers=_portal_headers(token),
    )
    if resp["status"] >= 400:
        j = resp.get("json") or {}
        msg = j.get("message") if isinstance(j, dict) else None
        return {"ok": False, "message": msg or f"HTTP {resp['status']}"}
    return {"ok": True, "message": "Cancelled.", "result": resp.get("json")}


def _membership_to_entity(m: dict) -> dict:
    """Tilefive membership → generic `membership` shape."""
    mt = m.get("membershipType") or {}
    # Tilefive's `isRecurring` is 1/0; `billingType` is opaque (e.g. "DOP").
    # `durationType` (YEAR/MONTH/WEEK) is a cleaner standard cadence.
    cadence = (mt.get("durationType") or "").lower() or None
    cadence_map = {"year": "annual", "month": "monthly", "week": "weekly"}
    return {
        "id": m["id"],
        "name": mt.get("name") or f"Membership {m['id']}",
        "tier": mt.get("name"),
        "status": m.get("status"),
        "startEffectiveDate": m.get("startEffectiveDate"),
        "endEffectiveDate": m.get("endEffectiveDate"),
        "nextBillDate": m.get("nextBillDate"),
        "autoRenew": bool(m.get("isRecurring")),
        "price": m.get("price"),
        "currency": "USD",
        "billingType": cadence_map.get(cadence, cadence),
        "useCount": m.get("useCount"),
        "guestPassQuantity": m.get("guestPassQuantity"),
        "content": mt.get("description"),
    }


def _pass_to_entity(p: dict) -> dict:
    """Tilefive pass → generic `pass` shape."""
    pt = p.get("passType") or {}
    status = p.get("status") or ("depleted" if p.get("quantity") == 0 else "active")
    return {
        "id": p["id"],
        "name": pt.get("name") or f"Pass {p['id']}",
        "status": status,
        "purchasedDate": p.get("purchasedDate") or p.get("createdAt"),
        "startEffectiveDate": p.get("startEffectiveDate"),
        "endEffectiveDate": p.get("endEffectiveDate") or p.get("endEffectiveDT"),
        "quantity": p.get("quantity"),
        "purchasedQuantity": p.get("purchasedQuantity"),
        "isAllDayPass": bool(p.get("isAllDayPass")),
        "depletedDate": p.get("depletedDate"),
        "price": p.get("price"),
        "currency": "USD",
    }


@test.skip(reason="needs credentials")
@returns("membership[]")
async def get_my_memberships(**params) -> list[dict]:
    """List memberships held by the logged-in account.

    Returns generic `membership`-shaped entities so agents can ask
    cross-skill questions like "what memberships do I have?"
    """
    raw = await _authed_get(params, "/customers/memberships")
    return [_membership_to_entity(m) for m in (raw or [])]


@test.skip(reason="needs credentials")
@returns("pass[]")
async def get_my_passes(**params) -> list[dict]:
    """List class passes held by the logged-in account.

    Returns generic `pass`-shaped entities.
    """
    raw = await _authed_get(params, "/customers/passes")
    return [_pass_to_entity(p) for p in (raw or [])]


@test.skip(reason="needs credentials")
@returns({"items": "array"})
async def get_my_bookings(**params) -> list[dict]:
    """List the authenticated user's upcoming reservations."""
    return await _authed_get(params, "/customers/bookings")
