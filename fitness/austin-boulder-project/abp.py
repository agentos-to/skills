"""Austin Boulder Project — Tilefive Portal API.

Two connections:
  - public: https://widgets.api.prod.tilefive.com (unauth — schedule, locations)
  - portal: https://portal.api.prod.tilefive.com  (authed — bookings, memberships)

Authentication: the portal is a CloudFront-fronted static SPA that
authenticates against AWS Cognito via `USER_PASSWORD_AUTH`. The `login`
tool resolves `{email, password}` from a credential provider
(`credentials.retrieve(".approach.app", required=["email","password"])`
— 1Password or any other `@provides(login_credentials)` skill),
runs the Cognito handshake, and persists the resulting `{email,
password, idToken, refreshToken}` in the credential store via
`__secrets__`. Authed tools read the IdToken off `params.auth`
and send it as the `Authorization` header.
"""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from agentos import (
    claims,
    client,
    connection,
    credentials,
    normalize_email,
    returns,
    skill_error,
    skill_secret,
    test,
)

connection("public",
    description="Tilefive widgets API — locations, schedule. No auth.",
    base_url="https://widgets.api.prod.tilefive.com",
    client="api")

connection("portal",
    description="Tilefive portal API — bookings, memberships, passes.",
    base_url="https://portal.api.prod.tilefive.com",
    domain=".approach.app",
    client="api",
    auth={"type": "api_key",
          "account": {"check":  "check_session",
                      "login":  "login",
                      "logout": "logout"}},
    label="ABP Portal Session",
    help_url="https://boulderingproject.portal.approach.app/login")

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

    html = await client.get(PORTAL_ORIGIN)
    if html["status"] >= 400:
        raise RuntimeError(f"portal HTML fetch failed: {html['status']}")
    m = _RE_BUNDLE_URL.search(html["body"] or "")
    if not m:
        raise RuntimeError("portal HTML has no app-*.js bundle reference")
    bundle_url = f"{PORTAL_ORIGIN}/assets/{m.group(1)}"

    bundle = await client.get(bundle_url, headers={
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

async def _cognito_initiate_auth(email: str, password: str) -> dict:
    """Run Cognito USER_PASSWORD_AUTH. Returns the full AuthenticationResult.

    Callers use `IdToken` as the portal bearer token and `RefreshToken`
    to mint fresh IdTokens without re-prompting for the password. The
    returned dict shape is Cognito's — `{IdToken, AccessToken,
    RefreshToken, ExpiresIn, TokenType}`.
    """
    if not email or not password:
        raise ValueError("email and password required for Cognito auth")
    cfg = await _discover_config()
    resp = await client.post(
        COGNITO_ENDPOINT,
        json={
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": cfg["cognitoClientId"],
            "AuthParameters": {
                "USERNAME": email.strip(),
                "PASSWORD": password.strip(),
            },
        },
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
    )
    if resp["status"] >= 400:
        raise RuntimeError(
            f"Cognito login failed: {resp['status']} "
            f"{(resp.get('body') or '')[:200]}"
        )
    return resp["json"]["AuthenticationResult"]


def _id_token_from_params(params: dict) -> str:
    """Read the IdToken the login tool persisted into the credential store.

    The engine's api_key auth resolver splats the credential row's
    `value` fields onto `params.auth`, so the `login` tool's
    `{email, password, idToken, refreshToken}` blob becomes
    `params.auth.idToken` etc.
    """
    auth = params.get("auth") or {}
    token = auth.get("idToken")
    if not token:
        raise RuntimeError(
            "No IdToken available — run the `login` tool first "
            "(agentos call run '{\"skill\":\"austin-boulder-project\","
            "\"tool\":\"login\"}')."
        )
    return token


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
    token = _id_token_from_params(params)
    resp = await client.get(
        f"{PORTAL_API}{path}",
        headers=_portal_headers(token),
        params=query,
    )
    if resp["status"] >= 400:
        raise RuntimeError(
            f"GET {path} -> {resp['status']}: {(resp.get('body') or '')[:200]}"
        )
    return resp["json"]


async def _current_customer_id(params: dict, token: str) -> int:
    """Portal endpoint that returns the authenticated user profile."""
    resp = await client.get(f"{PORTAL_API}/customers", headers=_portal_headers(token))
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
    resp = await client.get(f"{PORTAL_API}/customers/memberships", headers=_portal_headers(token))
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
        "at": "austin-boulder-project",   # namespace so membership.location stubs resolve
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


_RE_HTML_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"\s+")


def _strip_html(html: str | None) -> str | None:
    """Tilefive stores class descriptions as HTML blobs with inline
    styles. Strip tags for a plain-text rendering suitable for agent
    reasoning and terse UI. Keeps the prose; drops the markup noise.
    """
    if not html:
        return None
    text = _RE_HTML_TAG.sub(" ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = _RE_WS.sub(" ", text).strip()
    return text or None


def _booking_to_entity(b: dict) -> dict:
    event = b.get("event", {})
    activities = event.get("activitys") or []
    activity_name = activities[0].get("name", "") if activities else ""
    # Tilefive widget response uses `customerCount` (currently reserved)
    # and `event.maxCustomers` (capacity). There is no `ticketsRemaining`
    # field — an earlier version of this skill read it and silently got
    # `None`, causing every class to render as "full capacity available".
    taken = b.get("customerCount")
    capacity = event.get("maxCustomers") or b.get("maxNumOfGuests")
    spots = (capacity - taken) if (capacity is not None and taken is not None) else None
    full = spots == 0 if spots is not None else False
    desc = []
    if activity_name: desc.append(activity_name)
    if full: desc.append("FULL")
    elif spots is not None and capacity is not None: desc.append(f"{spots}/{capacity} spots")
    return {
        "id": b["id"],
        "name": b["name"],
        "content": " — ".join(desc),
        "description": _strip_html(b.get("description")),
        "startDate": b.get("startDT"),
        "endDate": b.get("endDT"),
        "timezone": AUSTIN_TZ_NAME,
        "activityType": activity_name,
        "capacity": capacity,
        "customerCount": taken,
        "spotsRemaining": spots,
        "isFull": full,
    }


# ---------------------------------------------------------------------------
# Operations — public connection (no credentials)
# ---------------------------------------------------------------------------

@returns("void")
@connection("public")
async def public_authenticate(*, force: bool = False, **params) -> dict:
    """Force re-reading widgetsApiKey + Cognito config from the live bundle.

    Public because these values are embedded in the portal's own JS bundle.
    """
    return await _discover_config(force=bool(force))


@test
@returns("place[]")
@connection("public")
async def get_locations(**params) -> list[dict]:
    """List all Bouldering Project locations as place entities.

    Austin has two — Springdale (id=6) and Westgate (id=5). Shape-
    typed so "what gyms are there?" works cross-skill.
    """
    cfg = await _discover_config()
    resp = await client.get(f"{WIDGETS_API}/locations", headers=_widgets_headers(cfg["widgetsApiKey"]))
    if resp["status"] >= 400:
        raise RuntimeError(f"/locations -> {resp['status']}")
    # Tilefive returns {"data": [...]} or raw [...]; handle both
    raw = resp["json"]
    rows = raw.get("data") if isinstance(raw, dict) else raw
    return [_location_to_entity(loc) for loc in (rows or [])]


@test
@returns("class[]")
@connection("public")
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
    resp = await client.get(
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
@connection("portal")
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
    token = _id_token_from_params(params)
    customer_id = await _current_customer_id(params, token)
    if membership_id is None:
        membership_id = await _active_membership_id(token)
        if membership_id is None:
            return {
                "ok": False,
                "message": "No active membership or pass — purchase one at "
                "https://boulderingproject.portal.approach.app/ to book classes.",
            }
    resp = await client.post(
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
        msg = (j.get("message") if isinstance(j, dict) else None) or f"HTTP {resp['status']}: {body[:200]}"
        # The portal returns "Booking is already full" as its capacity
        # signal (via 404 on /bookings/{id}/customers). We enrich the
        # message with a suggestion to re-query the schedule so the
        # caller sees current fullness rather than assuming stale state.
        if "full" in msg.lower():
            msg = (
                f"{msg} Class {booking_instance_id} is at capacity. "
                "Call `get_schedule` to see other times with open spots."
            )
        return {"ok": False, "message": msg, "booking_instance_id": booking_instance_id}
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
@connection("portal")
async def cancel_booking(booking_instance_id: int, reservation_id: int, **params) -> dict:
    """Cancel a class reservation (reservation_id comes from book_class result or get_my_bookings)."""
    token = _id_token_from_params(params)
    resp = await client.delete(
        f"{PORTAL_API}/bookings/{int(booking_instance_id)}/reservations/{int(reservation_id)}",
        headers=_portal_headers(token),
    )
    if resp["status"] >= 400:
        j = resp.get("json") or {}
        msg = j.get("message") if isinstance(j, dict) else None
        return {"ok": False, "message": msg or f"HTTP {resp['status']}"}
    return {"ok": True, "message": "Cancelled.", "result": resp.get("json")}


def _email_from_credentials(params: dict) -> str | None:
    """Return the authed account's email.

    After Phase 1, the engine splats the credential row's value fields
    onto `params.auth`, so `params.auth.email` and
    `params.auth.identifier` both hold the canonical email.
    """
    auth = params.get("auth") or {}
    ident = auth.get("identifier") or auth.get("email")
    return str(ident) if ident else None


def _account_stub(email: str | None) -> dict | None:
    """Identity-stub for the account node — engine resolves by (at, identifier)."""
    if not email:
        return None
    return {"at": "austin-boulder-project", "identifier": email}


def _location_stub(location_id) -> dict | None:
    """Identity-stub for an ABP location place node."""
    if location_id is None:
        return None
    return {"at": "austin-boulder-project", "id": location_id}


def _membership_to_entity(m: dict, email: str | None = None) -> dict:
    """Tilefive membership → generic `membership` shape."""
    mt = m.get("membershipType") or {}
    # Tilefive's `isRecurring` is 1/0; `billingType` is opaque (e.g. "DOP").
    # `durationType` (YEAR/MONTH/WEEK) is a cleaner standard cadence.
    cadence = (mt.get("durationType") or "").lower() or None
    cadence_map = {"year": "annual", "month": "monthly", "week": "weekly"}
    out = {
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
    acct = _account_stub(email)
    if acct: out["account"] = acct
    loc = _location_stub(m.get("purchasedLocationId"))
    if loc: out["location"] = loc
    return out


def _pass_to_entity(p: dict, email: str | None = None) -> dict:
    """Tilefive pass → generic `pass` shape."""
    pt = p.get("passType") or {}
    status = p.get("status") or ("depleted" if p.get("quantity") == 0 else "active")
    out = {
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
    acct = _account_stub(email)
    if acct: out["account"] = acct
    loc = _location_stub(p.get("purchasedLocationId"))
    if loc: out["location"] = loc
    return out


@test.skip(reason="needs credentials")
@returns("membership[]")
@connection("portal")
async def get_my_memberships(include_expired: bool = False, **params) -> list[dict]:
    """List memberships held by the logged-in account.

    Emitted memberships link to both the `account` (ABP login) and
    the `location` (gym branch) so "what memberships do I have?" and
    "which gym?" work cross-skill on the graph.

    Args:
        include_expired: when false (default), filter to `status=="active"`
            memberships only. Historical/cancelled/expired rows clutter
            the common "what am I paying for" query; callers who want the
            full history pass `include_expired=true`.
    """
    email = _email_from_credentials(params)
    raw = await _authed_get(params, "/customers/memberships")
    rows = raw or []
    if not include_expired:
        rows = [m for m in rows if (m.get("status") or "").lower() == "active"]
    return [_membership_to_entity(m, email) for m in rows]


@test.skip(reason="needs credentials")
@returns("pass[]")
@connection("portal")
async def get_my_passes(**params) -> list[dict]:
    """List class passes held by the logged-in account."""
    email = _email_from_credentials(params)
    raw = await _authed_get(params, "/customers/passes")
    return [_pass_to_entity(p, email) for p in (raw or [])]


@test.skip(reason="needs credentials")
@returns({"items": "array"})
@connection("portal")
async def get_my_bookings(**params) -> list[dict]:
    """List the authenticated user's upcoming reservations."""
    return await _authed_get(params, "/customers/bookings")


# ---------------------------------------------------------------------------
# Identity — account.check + login
# ---------------------------------------------------------------------------

_ABP = {
    "shape": "organization",
    "name": "Austin Boulder Project",
    "url": "https://austinboulderingproject.com",
}


@test.skip(reason="destructive or unsupported — migrated from yaml")
@returns("account")
@claims("primary_user")
@connection("portal")
async def check_session(**params) -> dict[str, Any]:
    """Verify the portal session and return the authed identity.

    Calls `/customers` on the portal API with the stored IdToken. The
    response includes the Cognito subject plus the account email; the
    email is the canonical identifier.
    """
    auth = params.get("auth") or {}
    token = auth.get("idToken")
    if not token:
        return {"authenticated": False}
    resp = await client.get(
        f"{PORTAL_API}/customers",
        headers=_portal_headers(token),
    )
    if resp["status"] >= 400:
        return {"authenticated": False}

    customer = resp["json"]
    canonical = normalize_email(customer["email"])
    display = " ".join(
        p for p in (customer.get("firstName"), customer.get("lastName")) if p
    ).strip()
    return {
        "authenticated": True,
        "at": _ABP,
        "identifier": canonical,
        "email": canonical,
        "displayName": display,
        "userId": str(customer["id"]),
    }


@returns({"status": "string", "identifier": "string"})
@connection("public")
async def login(*, email: str = "", password: str = "", **params) -> dict[str, Any]:
    """Log in to the ABP portal and persist a session for reuse.

    Credential resolution order:
      1. Caller passed `email` + `password` explicitly.
      2. `credentials.retrieve(".approach.app", required=["email","password"])`
         matchmakes an installed `@provides(login_credentials)` skill
         (1Password, Keychain, etc.).
      3. Nothing matched → structured `NeedsCredentials` error; agent
         surfaces "add it to your password manager, or pass it directly."

    On success, the skill runs the Cognito USER_PASSWORD_AUTH handshake
    and persists `{email, password, idToken, refreshToken}` via the
    `__secrets__` envelope under `(.approach.app, email)`. Authed tools
    read the IdToken from `params.auth.idToken` on subsequent calls.
    """
    if not email or not password:
        creds = await credentials.retrieve(
            domain=".approach.app",
            required=["email", "password"],
        )
        if creds and creds.get("found"):
            val = creds.get("value") or {}
            email = email or val.get("email") or ""
            password = password or val.get("password") or ""

    if not email or not password:
        return skill_error(
            "Missing credentials for .approach.app. Add an ABP login "
            "item to 1Password / Keychain, or call login() with "
            "email= and password= directly.",
            code="NeedsCredentials",
            domain=".approach.app",
            required=["email", "password"],
            help_url="https://boulderingproject.portal.approach.app/login",
        )

    result = await _cognito_initiate_auth(email, password)
    canonical = normalize_email(email)
    secret = skill_secret(
        domain=".approach.app",
        identifier=canonical,
        item_type="login_credentials",
        value={
            "email": canonical,
            "password": password,
            "idToken": result["IdToken"],
            "refreshToken": result.get("RefreshToken"),
            "accessToken": result.get("AccessToken"),
            "expiresIn": result.get("ExpiresIn"),
        },
        source="austin-boulder-project",
        metadata={
            "masked": {
                "password": "••••••••",
                "idToken": f"•••{result['IdToken'][-6:]}",
            },
            "tokenType": result.get("TokenType"),
        },
    )
    return {
        "__secrets__": [secret],
        "__result__": {
            "status": "authenticated",
            "identifier": canonical,
        },
    }


@test.skip(reason="destructive — revokes the live Cognito session")
@returns({"ok": "boolean", "message": "string"})
@connection("portal")
async def logout(**params) -> dict[str, Any]:
    """Revoke the current Cognito session via `GlobalSignOut`.

    `GlobalSignOut` invalidates every IdToken / AccessToken for this
    user across all devices — correct for "log out" semantics. After
    it returns, any token we persisted or handed out becomes dead at
    Cognito; the access token's ~1h natural TTL is the only
    remaining validity window. The refresh token is dead immediately.

    The engine runs the cleanup tail (delete skill-written credential
    rows, invalidate cache) after this returns, so we don't touch
    `__secrets__` here. Provider rows (1Password) stay put — logout
    forgets the session, not the password.

    Idempotent: a second call hits `NotAuthorizedException` which we
    treat as success — the session was already revoked.
    """
    auth = params.get("auth") or {}
    access_token = auth.get("accessToken")
    if not access_token:
        # No live session to revoke — engine's cleanup tail still runs.
        # Report ok=false so `revoked_server_side` doesn't lie: we didn't
        # actually talk to Cognito.
        return {"ok": False, "message": "No live access token; skipped server revoke."}

    resp = await client.post(
        COGNITO_ENDPOINT,
        json={"AccessToken": access_token},
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.GlobalSignOut",
        },
    )

    # Cognito quirk: already-revoked / expired tokens return 400
    # NotAuthorizedException. That's "done," not "failed."
    if resp["status"] == 200:
        return {"ok": True, "message": "Cognito session revoked."}

    body = (resp.get("body") or "")[:200]
    j = resp.get("json") or {}
    err_type = j.get("__type") if isinstance(j, dict) else None
    if err_type == "NotAuthorizedException":
        return {"ok": True, "message": "Session already expired at Cognito."}

    return {
        "ok": False,
        "message": f"Cognito GlobalSignOut failed: HTTP {resp['status']} {err_type or body}",
    }
