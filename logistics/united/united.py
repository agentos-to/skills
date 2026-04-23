"""
United Airlines skill — profile, MileagePlus balances, reservations,
flight search, and travel history.

Auth mechanics (see requirements.md for the full story):
- Session cookies live on `.united.com`. Critical cookies: AuthCookie,
  Session, User, PIM-SESSION-ID, 1pc_session, _ucid, plus Akamai
  bot-manager cookies (_abck, bm_*, ak_bmsc, akacd_*) which must pass
  through unchanged.
- Every API call wants `X-Authorization-api: bearer <hash>`. The bearer
  is minted by GET /api/auth/anonymous-token — misleading name; with
  cookies present it returns a USER-SCOPED token. Short-lived (~30min).

Cookie sourcing:
The default path reads cookies from the brave-browser provider (Brave's
on-disk SQLite). That's often stale relative to what Brave has in memory
— Brave flushes cookies lazily. To sidestep, this skill provides:

- `store_session_cookies(cookies=<dict>)` — manual: caller passes the
  fresh cookie values (maybe grabbed via CDP or the user's browser
  devtools), we validate against /User/profile and persist via
  __secrets__. The engine's credential store then becomes the freshest
  source for future calls.

- `login(cdp_port=9222)` — auto: connects to a live Brave/Chrome via
  CDP, reads the in-memory cookies (no SQLite lag), validates, persists.
  Requires Brave launched with --remote-debugging-port.

Both live at the engine's credential-store tier (not Brave's), so
staleness of Brave's disk DB becomes irrelevant.
"""

import json as _json

from agentos import client, connection, returns, timeout, skill_secret, skill_result


connection(
    "web",
    description="united.com session — flights, reservations, MileagePlus",
    base_url="https://www.united.com",
    client="browser",
    auth={"type": "cookies", "domain": ".united.com",
          "account": {"check": "check_session"}},
    label="United Session",
    help_url="https://www.united.com/en/us/account/sign-in")


# ── internal helpers ──────────────────────────────────────────────────────────

async def _mint_bearer() -> str | None:
    """Mint a user-scoped bearer via /api/auth/anonymous-token.

    Despite the name, this endpoint inspects the session cookies on the
    ambient jar and returns a user-scoped token if they're valid. Returns
    None if the session is anonymous or the mint fails.
    """
    resp = await client.get("https://www.united.com/api/auth/anonymous-token", client="fetch")
    if resp["status"] != 200:
        return None
    data = (resp["json"] or {}).get("data") or {}
    return (data.get("token") or {}).get("hash")


_BASE = "https://www.united.com"


async def _authed_get(path: str, **kwargs) -> dict:
    """GET an authenticated API path. Mints bearer, passes cookies."""
    bearer = await _mint_bearer()
    if not bearer:
        raise RuntimeError("SESSION_EXPIRED: united.com cookies are stale or anonymous")
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US",
        "X-Authorization-api": f"bearer {bearer}",
    }
    headers.update(kwargs.pop("headers", {}))
    url = path if path.startswith("http") else f"{_BASE}{path}"
    return await client.get(url, client="fetch", headers=headers, **kwargs)


async def _authed_post(path: str, body=None, **kwargs) -> dict:
    """POST an authenticated API path with JSON body."""
    bearer = await _mint_bearer()
    if not bearer:
        raise RuntimeError("SESSION_EXPIRED: united.com cookies are stale or anonymous")
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US",
        "Content-Type": "application/json",
        "X-Authorization-api": f"bearer {bearer}",
    }
    headers.update(kwargs.pop("headers", {}))
    url = path if path.startswith("http") else f"{_BASE}{path}"
    return await client.post(url, client="fetch", headers=headers, json=body, **kwargs)


# The United org node — reused as identity namespace for account / membership
_UNITED_ORG = {
    "shape": "airline",
    "name": "United Airlines",
    "url": "https://www.united.com",
    "iataCode": "UA",
    "icaoCode": "UAL",
    "callsign": "UNITED",
    "alliance": "Star Alliance",
    "country": "US",
}


def _prefer(*vals):
    """Return the first non-empty value."""
    for v in vals:
        if v not in (None, "", 0, []):
            return v
    return None


# United's AccountStatus → our membership.status vocabulary.
_ACCOUNT_STATUS_MAP = {
    "OPEN": "active",
    "ACTIVE": "active",
    "CLOSED": "cancelled",
    "SUSPENDED": "paused",
    "PENDING": "pending",
    "REVOKED": "revoked",
    "INACTIVE": "expired",
}


def _map_account_status(raw: str | None) -> str:
    if not raw:
        return "active"
    return _ACCOUNT_STATUS_MAP.get(raw.upper(), "active")


def _elite_tier(traveler: dict) -> str | None:
    """Read MileagePlus tier. Returns None when not elite — don't invent
    a value. Map from the canonical places United stores it."""
    elite = traveler.get("EliteDetails") or {}
    return _prefer(elite.get("EliteStatus"), elite.get("Tier"))


# ── tools ─────────────────────────────────────────────────────────────────────


@returns("account")
@connection("web")
@timeout(30)
async def store_session_cookies(*, cookies: dict, **params) -> dict:
    """Store a dict of cookie name→value pairs as the engine's canonical
    United session. Validates against /User/profile, then persists via
    __secrets__ so future calls use these cookies regardless of what the
    brave-browser provider's SQLite DB says.

    This is the recommended path when Brave's on-disk cookie DB is stale
    relative to its in-memory state — the user is logged in in Brave, but
    the skill keeps reading an older snapshot. The agent grabs fresh
    cookies via CDP (or pastes them from browser devtools) and hands them
    to us.

    Args:
        cookies: Dict like {"AuthCookie": "...", "Session": "...", ...}.
          Must include the auth-tier trio: AuthCookie, Session, User.
    """
    if not cookies:
        return skill_result(error="cookies dict is required")

    needed = {"AuthCookie", "Session", "User"}
    missing = needed - set(cookies.keys())
    if missing:
        return skill_result(error=f"missing required cookie(s): {sorted(missing)}")

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    # Mint a bearer using the caller-supplied cookies (not the engine's ambient jar).
    mint = await client.get(
        f"{_BASE}/api/auth/anonymous-token",
        client="fetch",
        cookies=cookie_header,
    )
    if mint.get("status") != 200:
        return skill_result(error=f"anonymous-token mint failed: status={mint.get('status')}")
    bearer = ((mint.get("json") or {}).get("data") or {}).get("token", {}).get("hash")
    if not bearer:
        return skill_result(error="anonymous-token mint returned no hash")

    # Verify the bearer is user-scoped by hitting /User/profile
    probe = await client.get(
        f"{_BASE}/xapi/myunited/User/profile",
        client="fetch",
        cookies=cookie_header,
        headers={
            "Accept": "application/json",
            "X-Authorization-api": f"bearer {bearer}",
        },
    )
    if probe.get("status") != 200:
        return skill_result(
            error=(f"cookies validated as anonymous, not user-scoped "
                   f"(profile returned {probe.get('status')}). "
                   f"These cookies don't represent a logged-in session.")
        )

    data = ((probe.get("json") or {}).get("data") or {})
    prof = data.get("profile") or {}
    traveler = (prof.get("Travelers") or [{}])[0]
    mp_id = traveler.get("MileagePlusId") or ""
    customer_id = traveler.get("CustomerId") or prof.get("CustomerId")
    display_name = traveler.get("CustomerName") or ""

    if not mp_id:
        return skill_result(error="profile returned 200 but no MileagePlusId")

    return {
        "__secrets__": [skill_secret(
            domain=".united.com",
            identifier=mp_id,
            item_type="cookie",
            value=cookies,
            source="united",
            label=f"United Session ({mp_id})",
            metadata={"united": {"customerId": str(customer_id) if customer_id else None,
                                 "displayName": display_name}},
        )],
        # Shape-conformant account for graph upsert (per check_session convention)
        "id": f"united:{mp_id}",
        "issuer": "united.com",
        "identifier": mp_id,
        "handle": mp_id,
        "displayName": display_name,
        "accountType": "mileageplus",
        "isActive": True,
        "at": {"shape": "airline", "name": "United Airlines",
               "url": "https://www.united.com", "iataCode": "UA"},
    }


@returns("account")
@connection("web")
@timeout(30)
async def check_session(**params) -> dict:
    """Verify the United session is active.

    Returns an account node with the logged-in MileagePlus identity, or
    raises SESSION_EXPIRED if the cookies are stale.
    """
    bearer = await _mint_bearer()
    if not bearer:
        raise RuntimeError("SESSION_EXPIRED: no bearer could be minted")

    # Don't trust /api/auth/validate-token — it returns {valid: false} for
    # BOTH "actually expired" and "anonymously scoped" bearers. Since
    # anonymous-token endpoint can return an anonymous bearer when cookies
    # are too stale to mint a user-scoped token, validate-token can't
    # distinguish. Instead, hit the real user endpoint: if /User/profile
    # returns a CustomerId, we're user-authenticated. 403 = not.
    resp = await client.get(
        f"{_BASE}/xapi/myunited/User/profile",
        client="fetch",
        headers={
            "Accept": "application/json",
            "Accept-Language": "en-US",
            "X-Authorization-api": f"bearer {bearer}",
        },
    )
    status = resp.get("status")
    if status == 403:
        raise RuntimeError(
            "SESSION_EXPIRED: United returned 403 on /User/profile. The bearer "
            "is live but ANONYMOUSLY scoped (cookies present, server session "
            "stale). Browser cookies need a flush — interact with united.com "
            "in Brave (click a page that hits /xapi/), wait ~30s, retry. "
            "If that fails, sign back in at "
            "https://www.united.com/en/us/account/sign-in"
        )
    if status != 200:
        raise RuntimeError(f"United profile call returned {status}: {resp.get('body', '')[:200]}")

    data = ((resp.get("json") or {}).get("data") or {})
    traveler = ((data.get("profile") or {}).get("Travelers") or [{}])[0]
    mp_id = traveler.get("MileagePlusId") or ""
    customer_id = traveler.get("CustomerId") or (data.get("profile") or {}).get("CustomerId")

    return {
        "id": f"united:{mp_id or customer_id}",
        "issuer": "united.com",
        "identifier": mp_id or str(customer_id or ""),
        "handle": mp_id,
        "displayName": traveler.get("CustomerName") or "",
        "accountType": "mileageplus",
        "isActive": True,
        "at": {"shape": "airline", "name": "United Airlines",
               "url": "https://www.united.com", "iataCode": "UA"},
    }


@returns("person")
@connection("web")
@timeout(30)
async def get_profile(**params) -> dict:
    """Fetch the logged-in user's full profile: legal name, MileagePlus,
    title, addresses, phones.

    Returns a person node with nested account + membership relations.
    """
    resp = await _authed_get("/xapi/myunited/User/profile")
    data = ((resp.get("json") or {}).get("data") or {})
    prof = data.get("profile") or {}
    traveler = (prof.get("Travelers") or [{}])[0]

    given = traveler.get("FirstName") or ""
    additional = traveler.get("MiddleName") or ""
    family = traveler.get("LastName") or ""
    honorific = (traveler.get("Title") or "").rstrip(".")  # "Mr." → "Mr"
    mp_id = traveler.get("MileagePlusId") or ""
    customer_id = traveler.get("CustomerId") or prof.get("CustomerId")

    full_name = " ".join(p for p in [given, additional, family] if p) or traveler.get("CustomerName") or ""

    person = {
        "id": f"united-customer:{customer_id}" if customer_id else f"united-mp:{mp_id}",
        "name": full_name,
        "givenName": given,
        "additionalName": additional,
        "familyName": family,
        "honorificPrefix": honorific or None,
        # United stores the spelling the user entered at signup; treat it as
        # the source of truth for what's on their MileagePlus profile (and
        # thus what prints on United tickets). A user can override by
        # editing their profile on united.com.
        "legalName": full_name or None,
        "url": "https://www.united.com/en/us/account",
        "accounts": [{
            "id": f"united:{mp_id or customer_id}",
            "issuer": "united.com",
            "identifier": mp_id or str(customer_id or ""),
            "handle": mp_id,
            "displayName": full_name,
            "accountType": "mileageplus",
            "at": {"shape": "airline", "name": "United Airlines",
                   "url": "https://www.united.com", "iataCode": "UA"},
        }],
    }

    if mp_id:
        person["memberships"] = [{
            "id": mp_id,
            "name": f"MileagePlus {mp_id}",
            "status": "active",
            "tier": _elite_tier(traveler),
            "at": {"shape": "airline", "name": "United Airlines",
                   "url": "https://www.united.com", "iataCode": "UA"},
        }]

    return person


@returns("membership")
@connection("web")
@timeout(30)
async def get_mileageplus(**params) -> dict:
    """Fetch current MileagePlus membership with up-to-date miles balance,
    Premier Qualifying Points, and tier.
    """
    prof_resp, bal_resp = None, None
    prof_resp = await _authed_get("/xapi/myunited/User/profile")
    bal_resp = await _authed_get("/api/myunited/user/balances")

    prof = ((prof_resp.get("json") or {}).get("data") or {}).get("profile") or {}
    traveler = (prof.get("Travelers") or [{}])[0]
    mp_id = traveler.get("MileagePlusId") or ""

    bal_data = (bal_resp.get("json") or {}).get("data") or {}
    balances = {b.get("ProgramCurrencyType"): b.get("TotalBalance")
                for b in (bal_data.get("Balances") or [])}
    pq_metrics = {b.get("ProgramCurrencyType"): b.get("Balance")
                  for b in (bal_data.get("PremierQualifyingMetrics") or [])}

    tier = _elite_tier(traveler)
    miles = int(balances.get("RDM") or 0)

    full_name = " ".join(p for p in [
        traveler.get("FirstName"), traveler.get("MiddleName"), traveler.get("LastName")
    ] if p) or traveler.get("CustomerName") or ""

    tier_label = tier or "Member"  # used for display only; real null still in `tier`

    return {
        "id": mp_id,
        "name": f"MileagePlus {mp_id}",
        "status": _map_account_status(bal_data.get("AccountStatus")),
        "tier": tier,
        "useCount": None,
        "published": None,
        "content": (
            f"MileagePlus member {mp_id}. "
            f"{miles:,} redeemable miles. Tier: {tier_label}."
        ),
        "at": {"shape": "airline", "name": "United Airlines",
               "url": "https://www.united.com", "iataCode": "UA"},
        "member": {
            "id": f"united-customer:{traveler.get('CustomerId')}",
            "name": full_name,
            "givenName": traveler.get("FirstName"),
            "additionalName": traveler.get("MiddleName"),
            "familyName": traveler.get("LastName"),
        },
        # Raw balance snapshot for downstream tools that want PQP / travel
        # bank / certificates without a second round trip.
        "_balances": balances,
        "_premier": pq_metrics,
    }


@returns("reservation[]")
@connection("web")
@timeout(45)
async def list_trips(upcoming_only: bool = True, **params) -> list[dict]:
    """List upcoming United reservations for the logged-in MileagePlus user.

    Args:
        upcoming_only: If True (default), fetch only future trips. If False,
          still returns upcoming today-to-next-year (United's MyTripsByMileagePlus
          endpoint doesn't return distant past trips — past-trip history lives
          elsewhere and isn't yet implemented).
    """
    from datetime import date, timedelta

    today = date.today()
    end = today + timedelta(days=365)
    body = {
        "NumberOfItineraries": 10,
        "StartDate": today.strftime("%m/%d/%Y"),
        "EndDate": end.strftime("%m/%d/%Y"),
    }
    resp = await _authed_post("/api/mytrips/MyTripsByMileagePlus/", body=body)
    data = (resp.get("json") or {}).get("Data") or []

    reservations: list[dict] = []
    for itin in data:
        # Shape mapping is a placeholder until we capture a non-empty response.
        # When we book a flight and capture the actual structure, refine this.
        pnr = itin.get("RecordLocator") or itin.get("ConfirmationNumber") or ""
        reservations.append({
            "id": f"united-pnr:{pnr}",
            "reservationType": "flight",
            "reservationId": pnr,
            "status": (itin.get("Status") or "confirmed").lower(),
            "bookingType": "instant",
            "name": f"United reservation {pnr}",
            "at": {"shape": "airline", "name": "United Airlines",
                   "url": "https://www.united.com", "iataCode": "UA"},
            "_raw": itin,
        })
    return reservations


# ── flight search ────────────────────────────────────────────────────────────


_CABIN_TO_PREF = {
    "economy": "economy",
    "premium_economy": "premium_economy",
    "business": "business",
    "first": "first",
}


def _price_of(product: dict) -> tuple[float | None, str | None]:
    """Pull (amount, currency) from a product's `prices[]`. Picks the
    'Fare' row — the all-in total the user sees on the results page."""
    for pr in product.get("prices") or []:
        if pr.get("pricingType") == "Fare":
            return pr.get("amount"), pr.get("currency")
    return None, None


def _iso_depart(local_str: str, tz_offset_hours: int | None) -> str | None:
    """Convert United's 'YYYY-MM-DD HH:MM' local-naive string + timezone
    offset to an ISO-8601 datetime with offset."""
    if not local_str:
        return None
    iso = local_str.replace(" ", "T")
    if tz_offset_hours is None:
        return iso
    sign = "+" if tz_offset_hours >= 0 else "-"
    h = abs(int(tz_offset_hours))
    return f"{iso}{sign}{h:02d}:00"


def _segment_to_flight(seg: dict) -> dict:
    """Convert one United flight segment (top-level or nested in
    `connections[]`) into an agentOS `flight` shape."""
    marketing = seg.get("marketingCarrier") or "UA"
    flight_number = seg.get("flightNumber") or ""
    equipment = seg.get("equipmentDisclosures") or {}
    return {
        "id": f"{marketing}{flight_number}:{seg.get('departDateTime', '')}",
        "name": f"{marketing} {flight_number}",
        "flightNumber": f"{marketing} {flight_number}",
        "departureTime": _iso_depart(seg.get("departDateTime"), seg.get("orgTimezoneOffset")),
        "arrivalTime": _iso_depart(seg.get("destinationDateTime"), seg.get("destTimezoneOffset")),
        "durationMinutes": seg.get("travelMinutes"),
        "airline": {
            "shape": "airline",
            "iataCode": marketing,
            "name": seg.get("marketingCarrierDescription") or "United Airlines",
            "url": "https://www.united.com" if marketing == "UA" else None,
        },
        "departsFrom": {
            "iataCode": seg.get("origin"),
            "name": seg.get("origin"),
        },
        "arrivesAt": {
            "iataCode": seg.get("destination"),
            "name": seg.get("destination"),
        },
        "aircraft": {
            "icaoCode": equipment.get("equipmentType") or None,
            "model": equipment.get("equipmentDescription") or None,
        } if equipment.get("equipmentType") else None,
    }


def _flight_to_trip(f: dict) -> dict:
    """Convert United's top-level `flight` (one itinerary option, possibly
    multi-segment) into an agentOS `trip` shape with `legs: flight[]`."""
    first = _segment_to_flight(f)
    legs = [first]
    for conn in f.get("connections") or []:
        legs.append(_segment_to_flight(conn))

    last_seg = (f.get("connections") or [None])[-1] or f
    carrier = f.get("marketingCarrier") or "UA"
    flight_numbers = "/".join(
        f"{seg.get('marketingCarrier') or 'UA'} {seg.get('flightNumber') or '?'}"
        for seg in [f, *(f.get("connections") or [])]
    )
    trip_type = "nonstop" if not (f.get("connections") or []) else f"{len(legs) - 1}-stop"

    return {
        "id": f"ua-trip:{f.get('hash') or first['id']}",
        "name": f"{f.get('origin')}→{last_seg.get('destination')} — {flight_numbers}",
        "tripType": trip_type,
        "status": "offered",
        "departureTime": _iso_depart(f.get("departDateTime"), f.get("orgTimezoneOffset")),
        "arrivalTime": _iso_depart(last_seg.get("destinationDateTime"), last_seg.get("destTimezoneOffset")),
        "durationMinutes": f.get("travelMinutesTotal") or f.get("travelMinutes"),
        "stops": len(f.get("connections") or []),
        "cabinClass": None,  # populated from the product that an offer wraps
        "carrier": {
            "shape": "airline",
            "iataCode": carrier,
            "name": "United Airlines",
            "url": "https://www.united.com",
        },
        "origin": {
            "iataCode": f.get("origin"),
            "name": f.get("origin"),
        },
        "destination": {
            "iataCode": last_seg.get("destination"),
            "name": last_seg.get("destination"),
        },
        "legs": legs,
    }


@returns("offer[]")
@connection("web")
@timeout(60)
async def search_flights(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str = None,
    passengers: int = 1,
    cabin: str = "economy",
    include_basic: bool = True,
    award: bool = False,
    **params,
) -> list:
    """Search United flights. Returns an `offer[]` — one per fare bucket per
    flight option, with a nested `trips[]` relation whose `legs[]` are the
    individual flight segments.

    Outbound only; round-trip needs a second call with the chosen outbound's
    cartId passed through (not yet wired). For now, calling with `return_date`
    just ignores it; do two separate calls.

    Args:
        origin: IATA code (e.g. 'AUS').
        destination: IATA code (e.g. 'SFO').
        depart_date: YYYY-MM-DD.
        return_date: Ignored in v1 (single-slice search). Reserved for future.
        passengers: Count of adult passengers.
        cabin: economy, premium_economy, business, or first.
        include_basic: If True, surfaces Basic Economy as a fare option.
        award: If True, searches with MileagePlus miles pricing.
    """
    cabin_pref = _CABIN_TO_PREF.get(cabin.lower(), "economy")
    fare_family = "ECONOMY" if cabin_pref == "economy" else cabin_pref.upper()

    body = {
        "SearchTypeSelection": 1,
        "SortType": "bestmatches",
        "SortTypeDescending": False,
        "Trips": [{
            "Origin": origin.upper(),
            "Destination": destination.upper(),
            "DepartDate": depart_date,
            "Index": 1,
            "TripIndex": 1,
            "SearchRadiusMilesOrigin": 0,
            "SearchRadiusMilesDestination": 0,
            "DepartTimeApprox": 0,
            "SearchFiltersIn": {
                "FareFamily": fare_family,
                "AirportsStop": None,
                "AirportsStopToAvoid": None,
                "ShopIndicators": {
                    "IsTravelCreditsApplied": False,
                    "IsDoveFlow": True,
                },
            },
        }],
        "CabinPreferenceMain": cabin_pref,
        "PaxInfoList": [{"PaxType": 1} for _ in range(max(1, passengers))],
        "AwardTravel": bool(award),
        "NGRP": False,
        "CalendarLengthOfStay": 0,
        "PetCount": 0,
        "RecentSearchKey": f"{origin.upper()}{destination.upper()}{depart_date}",
        "CalendarFilters": {"Filters": {"PriceScheduleOptions": {"Stops": 1}}},
        "Characteristics": [
            {"Code": "SOFT_LOGGED_IN", "Value": False},
            {"Code": "UsePassedCartId", "Value": False},
        ],
        "FareType": "Refundable",
        "BuildHashValue": "true",
        "EnableBasicPremiumProducts": bool(include_basic),
    }

    bearer = await _mint_bearer()
    if not bearer:
        raise RuntimeError("SESSION_EXPIRED: no bearer could be minted")

    # The endpoint streams SSE — we accept that the engine's HTTP client
    # may deliver the whole body at once (buffered) or line-by-line.
    # Either way, the body arrives as a single string we parse.
    resp = await client.post(
        f"{_BASE}/api/flight/FetchSSENestedFlights",
        client="fetch",
        json=body,
        headers={
            "Accept": "text/event-stream",
            "Accept-Language": "en-US",
            "Content-Type": "application/json",
            "X-Authorization-api": f"bearer {bearer}",
        },
    )
    raw = resp.get("body") or ""
    if resp.get("status") not in (200, 201):
        raise RuntimeError(f"United flight search failed: status={resp.get('status')} body={raw[:400]}")

    # Parse SSE events
    events: list[dict] = []
    for chunk in raw.split("\n\n"):
        chunk = chunk.strip()
        if not chunk.startswith("data:"):
            continue
        try:
            events.append(_json.loads(chunk[5:].strip()))
        except Exception:
            continue

    # Pull cartId from the meta event (first one)
    cart_id = None
    for e in events:
        if e.get("type") == "meta":
            cart_id = e.get("cartId")
            break

    # Flatten: one offer per (trip × fare product). Trip is the journey
    # (one or more legs); the offer wraps it with a price + bookingToken.
    offers: list[dict] = []
    for e in events:
        if e.get("type") != "flightOption":
            continue
        flight = e.get("flight") or {}
        trip = _flight_to_trip(flight)

        # Walk products and their nestedProducts. Each is its own offer.
        def _emit(product: dict, parent_id: str | None = None):
            amount, currency = _price_of(product)
            product_id = product.get("productId")
            if not product_id:
                return
            trip_label = trip["name"]
            fare_label = (product.get("title") or "") + (
                f" — {product.get('subTitle')}" if product.get("subTitle") else ""
            )
            offers.append({
                "id": f"united-offer:{product_id}",
                "name": f"{trip_label}  {fare_label}".strip(),
                "price": amount,
                "currency": currency,
                "offerType": "award" if award else "revenue",
                "availability": "available" if amount is not None else "unavailable",
                "bookingToken": product_id,
                "offeredBy": {
                    "shape": "airline",
                    "iataCode": "UA",
                    "name": "United Airlines",
                    "url": "https://www.united.com",
                },
                # Offer → Trip relation (the canonical offer shape). A trip
                # has legs (flight[]); connecting itineraries show up
                # naturally as multi-leg trips.
                "trips": [{**trip, "cabinClass": (product.get("cabinType") or "").lower() or None}],
                # Non-shape passthrough — useful for downstream tools (select,
                # price, checkout) that need to echo United's internal ids.
                "_fareFamily": product.get("fareFamily"),
                "_bookingCode": product.get("bookingCode"),
                "_productType": product.get("productType"),
                "_fareBasisCode": ((product.get("fares") or [{}])[0].get("fareBasisCode")),
                "_flightHash": flight.get("hash"),
                "_cartId": cart_id,
                "_parentProductId": parent_id,
            })
            for np in product.get("nestedProducts") or []:
                _emit(np, parent_id=product_id)

        for p in flight.get("products") or []:
            _emit(p)

    return offers


# ── booking flow tools ───────────────────────────────────────────────────────
#
# The booking flow (post-search) is:
#   1. select_flight(cart_id, booking_token, flight_hash)
#      → POST /api/flight/RegisterFlights
#      → commits the outbound selection, response carries a DisplayCart
#        with fare + taxes + traveler placeholders.
#   2. register_traveler(cart_id, traveler_data)
#      → POST /api/ShoppingCart/RegisterTravelers
#      → commits passenger details (name, DOB, contact, KTN, Loyalty).
#   3. get_seatmap(cart_id, flight_hash, ...)
#      → POST /api/SeatMap/Retrieve
#      → returns the full cabin layout with pricing + availability.
#   4. (TBD) register_seats, apply_offers, checkout
#
# The skill stops short of checkout — we never submit payment.


def _bbx_cell_id(booking_token: str, fare_family: str | None = None) -> str:
    """Construct a BBXCellId from a search-time productId.

    Captured: search gave productId ending in `002` (Basic Economy ECO-BASIC);
    RegisterFlights needed BBXCellId ending in `006`. Suffix 006 = fare column
    id. Exact mapping from fare_family → suffix needs more clicks to pin down.
    For now, trust the caller to pass a properly-suffixed id (we echo it as-is).

    TODO: when we capture more fare-column clicks, implement suffix remap
    table here.
    """
    return booking_token


@returns("reservation")
@connection("web")
@timeout(45)
async def select_flight(
    *,
    cart_id: str,
    booking_token: str,
    flight_hash: str,
    fare_type: str = "Refundable",
    **params,
) -> dict:
    """Commit a flight selection — the step between search and traveler-info.

    Hits `/api/flight/RegisterFlights` with the cart id + booking token
    from `search_flights`. Returns a `reservation` stub with the cart
    state: total amount, fare breakdown, selected flight. The reservation
    isn't "booked" yet (no PNR) — it's a held cart, good for ~5 min
    before the checkout session idle-times out.

    Args:
        cart_id: UUID from the search meta event (carry over).
        booking_token: productId from the chosen offer (search result).
        flight_hash: hash from the chosen flight option (e.g. "118-1336-UA").
        fare_type: "Refundable" (default) or "NonRefundable".
    """
    body = {
        "CartId": cart_id,
        "BBXCellId": _bbx_cell_id(booking_token),
        "MoneyAndMilesOptionId": None,
        "BBXSolutionSetId": None,
        "flightHash": flight_hash,
        "RequeryForUpsell": False,
        "CalendarFilters": {"Filters": {"PriceScheduleOptions": {"Stops": 1}}},
        "FareType": fare_type,
        "BuildHashValue": "true",
        "Characteristics": [
            {"Code": "IsNewRTI", "Value": "true"},
        ],
    }
    resp = await _authed_post("/api/flight/RegisterFlights", body=body)
    if resp.get("status") != 200:
        raise RuntimeError(
            f"RegisterFlights failed: status={resp.get('status')} "
            f"body={(resp.get('body') or '')[:300]}"
        )
    data = ((resp.get("json") or {}).get("Data") or {})
    dc = data.get("DisplayCart") or {}
    trips_raw = dc.get("DisplayTrips") or []

    # Build trip shapes from DisplayTrips — these are the selected legs
    trips = []
    for dt in trips_raw:
        flights = dt.get("Flights") or dt.get("DisplayFlights") or []
        legs = []
        for f in flights:
            legs.append({
                "id": f"UA{f.get('FlightNumber','')}:{f.get('DepartDateTime','')}",
                "flightNumber": f"UA {f.get('FlightNumber','')}",
                "departureTime": (f.get("DepartDateTime") or "").replace(" ", "T") or None,
                "arrivalTime": (f.get("ArrivalDateTime") or f.get("DestinationDateTime") or "").replace(" ", "T") or None,
                "airline": _UNITED_ORG,
                "departsFrom": {"iataCode": f.get("Origin"), "name": f.get("Origin")},
                "arrivesAt":  {"iataCode": f.get("Destination"), "name": f.get("Destination")},
            })
        first = flights[0] if flights else {}
        last  = flights[-1] if flights else {}
        trips.append({
            "id": f"ua-trip:{cart_id}:{dt.get('TripIndex') or '1'}",
            "name": f"{dt.get('Origin') or first.get('Origin','?')}→{dt.get('Destination') or last.get('Destination','?')}",
            "tripType": "flight",
            "status": "held",
            "departureTime": (first.get("DepartDateTime","") or "").replace(" ","T") or None,
            "arrivalTime":   (last.get("ArrivalDateTime", last.get("DestinationDateTime","")) or "").replace(" ","T") or None,
            "carrier": _UNITED_ORG,
            "origin":      {"iataCode": dt.get("Origin") or first.get("Origin"), "name": dt.get("Origin") or first.get("Origin")},
            "destination": {"iataCode": dt.get("Destination") or last.get("Destination"), "name": dt.get("Destination") or last.get("Destination")},
            "legs": legs,
        })

    return {
        "id": f"united-cart:{cart_id}",
        "reservationType": "flight",
        "reservationId": cart_id,    # pre-PNR: use cart id as reservation id
        "status": "hold",            # held cart, not yet confirmed
        "bookingType": "instant",
        "totalAmount": dc.get("GrandTotal"),
        "baseAmount":  (dc.get("DisplayPrices") or [{}])[0].get("Amount") if dc.get("DisplayPrices") else None,
        "currency": "USD",
        "at": _UNITED_ORG,
        "trips": trips,
        # Non-shape passthrough for downstream tools
        "_cartId": cart_id,
        "_bbxSolutionSetId": data.get("LastBBXSolutionSetId"),
        "_flightHash": flight_hash,
    }


@returns("reservation")
@connection("web")
@timeout(45)
async def register_traveler(
    *,
    cart_id: str,
    surname: str = None,
    given_name: str = None,
    middle_name: str = "",
    suffix: str = "",
    date_of_birth: str = None,      # MM/DD/YYYY
    sex: str = None,                 # "M" | "F"
    known_traveler_number: str = "",
    redress_number: str = "",
    email: str = None,
    phone_number: str = None,        # "5551234567" (10 digits, US)
    country_calling_code: str = "1",
    mileage_plus_id: str = None,
    loyalty_program_id: str = "7",   # 7 = United
    **params,
) -> dict:
    """Submit traveler info to the cart — step 2 of booking.

    Fills in the `/ShoppingCart/RegisterTravelers` payload. Any arg left
    as None will be pulled from the logged-in user's profile
    (`/xapi/myunited/User/profile`) — so a logged-in user booking for
    themselves can call `register_traveler(cart_id=...)` with nothing
    else.

    Args:
        cart_id: from select_flight response.
        surname, given_name, middle_name, suffix: passenger name as it
          should appear on the ticket. Defaults to profile.
        date_of_birth: MM/DD/YYYY. Defaults to profile.
        sex: "M"/"F". Defaults to profile GenderCode.
        known_traveler_number: TSA PreCheck / Global Entry / NEXUS ID.
        redress_number: DHS TRIP redress number (for no-fly-list appeals).
        email: contact email. Defaults to profile primary.
        phone_number: 10-digit US number. Defaults to profile primary mobile.
        country_calling_code: "1" for US, etc.
        mileage_plus_id: MP number to credit. Defaults to logged-in MP#.
        loyalty_program_id: "7" = United. For partner FFs, different id.
    """
    # Fetch profile if any arg is None — we need it for the defaults
    need_profile = any(v is None for v in (surname, given_name, date_of_birth, sex, email, phone_number, mileage_plus_id))
    if need_profile:
        prof_resp = await _authed_get("/xapi/myunited/User/profile")
        prof = ((prof_resp.get("json") or {}).get("data") or {}).get("profile") or {}
        t0 = (prof.get("Travelers") or [{}])[0]

        surname           = surname           or t0.get("LastName")
        given_name        = given_name        or t0.get("FirstName")
        middle_name       = middle_name or t0.get("MiddleName") or ""
        suffix            = suffix or t0.get("Suffix") or ""
        # DOB: profile has "1987-01-25T00:00:00"; United wants "01/25/1987"
        if not date_of_birth:
            raw = t0.get("BirthDate") or ""
            if raw:
                from datetime import datetime
                try:
                    date_of_birth = datetime.fromisoformat(raw.replace("Z","+00:00")).strftime("%m/%d/%Y")
                except Exception:
                    date_of_birth = raw[:10]
        sex = sex or t0.get("GenderCode")
        mileage_plus_id = mileage_plus_id or t0.get("MileagePlusId")

        if not email:
            em_resp = await _authed_get("/api/user/emailAddresses")
            em_list = ((em_resp.get("json") or {}).get("data") or {}).get("EmailAddresses") or []
            primary = next((e for e in em_list if e.get("IsPrimary")), (em_list[0] if em_list else {}))
            email = primary.get("EmailAddress")

        if not phone_number:
            ph_resp = await _authed_get("/api/user/phoneNumbers")
            ph_list = ((ph_resp.get("json") or {}).get("data") or {}).get("PhoneNumbers") or []
            primary = next((p for p in ph_list if p.get("IsPrimary")), (ph_list[0] if ph_list else {}))
            phone_number = primary.get("PhoneNumber")
            country_calling_code = primary.get("CountryPhoneNumber") or country_calling_code

        if not known_traveler_number:
            # Pull from travelerSupplementaryTravelInfo
            sup_resp = await _authed_get("/api/user/travelerSupplementaryTravelInfo")
            sup = ((sup_resp.get("json") or {}).get("data") or {}).get("SupplementaryTravelInfos") or []
            ktn = next((s.get("Number") for s in sup if s.get("Type") == "K"), "")
            known_traveler_number = ktn or ""

    # Build the body. Documents[] carries KTN as Type=15 (Secure Flight).
    documents = []
    if known_traveler_number:
        documents.append({
            "DateOfBirth": date_of_birth,
            "KnownTravelerNumber": known_traveler_number,
            "RedressNumber": redress_number or None,
            "CanadianTravelNumber": None,
            "GivenName": given_name,
            "MiddleName": middle_name,
            "Surname": surname,
            "Suffix": suffix,
            "Sex": sex,
            "Type": 15,  # Secure Flight passenger doc
        })

    body = {
        "Channel": "WEB",
        "PetTravelers": None,
        "Travelers": None,
        "WorkFlowType": 1,
        "IsUMNROptIn": False,
        "FlightTravelers": [{
            "OxygenFlowRate": 0,
            "TravelerNameIndex": "",
            "Traveler": {
                "Person": {
                    "Surname": surname,
                    "GivenName": given_name,
                    "MiddleName": middle_name,
                    "Suffix": suffix,
                    "DateOfBirth": date_of_birth,
                    "Sex": sex,
                    "Documents": documents,
                    "CountryOfResidence": {},   # profile may be stale; leave blank
                    "Nationality": [],
                    "Type": "ADT",
                    "InfantIndicator": "false",
                    "Contact": {
                        "Emails": [{"Address": email}],
                        "PhoneNumbers": [{
                            "Description": "H",
                            "CountryAccessCode": "US",     # ISO country; NOT the calling code
                            "AreaCityCode": str(country_calling_code),
                            "PhoneNumber": str(phone_number),
                        }],
                    },
                },
                "LoyaltyProgramProfile": {
                    "LoyaltyProgramCarrierCode": "UA",
                    "LoyaltyProgramMemberID": mileage_plus_id,
                    "LoyaltyProgramID": loyalty_program_id,
                    "LoyaltyProgramMemberTierLevel": None,
                },
            },
            "SpecialServiceRequests": [],
            "PtcList": None,
        }],
        "SpecialServiceRequest": None,
        "IsReserved": False,
        "Characteristics": [
            {"Code": "OMNICHANNELCART", "Value": True},
        ],
        "CartId": cart_id,
        "IsSessionFirst": False,
        "ReEvaluateExpressCheckout": False,
    }
    resp = await _authed_post("/api/ShoppingCart/RegisterTravelers", body=body)
    if resp.get("status") != 200:
        raise RuntimeError(
            f"RegisterTravelers failed: status={resp.get('status')} "
            f"body={(resp.get('body') or '')[:300]}"
        )

    # RegisterTravelers uses lowercase "data" (unlike RegisterFlights's "Data").
    js = resp.get("json") or {}
    data = js.get("data") or js.get("Data") or {}
    dc = data.get("DisplayCart") or {}
    return {
        "id": f"united-cart:{cart_id}",
        "reservationType": "flight",
        "reservationId": cart_id,
        "status": "hold",
        "bookingType": "instant",
        "totalAmount": dc.get("GrandTotal"),
        "currency": "USD",
        "at": _UNITED_ORG,
        "_cartId": cart_id,
        "_travelerAccepted": True,
    }


@returns("seatmap")
@connection("web")
@timeout(45)
async def get_seatmap(
    *,
    cart_id: str,
    flight_number: int,
    origin: str,
    destination: str,
    departure_datetime: str,       # "2026-04-28T13:00"
    arrival_datetime: str,          # "2026-04-28T15:02"
    class_of_service: str = "N",
    fare_basis_code: str = "",
    segment_number: int = 1,
    dod_cabins: list = None,
    **params,
) -> dict:
    """Fetch the full seat map for a selected flight.

    Returns a `seatmap` node with cabins[] (each with rows[], seats[],
    monumentRows[]), tiers[] pricing, and summary flags. Not a booking
    commitment — just the catalog.

    Args:
        cart_id: UUID from select_flight / register_traveler.
        flight_number: integer flight number (e.g. 1336).
        origin, destination: IATA codes.
        departure_datetime, arrival_datetime: "YYYY-MM-DDTHH:MM" local.
        class_of_service: booking RBD ("N" = Basic Economy).
        fare_basis_code: e.g. "LAA0AQBN" — from the chosen fare.
        segment_number: 1 for single-flight; increments for multi-segment.
        dod_cabins: ["J","O"] captures both First (J) and Economy (O).
    """
    import uuid
    if dod_cabins is None:
        dod_cabins = ["J", "O"]

    # Look up traveler info from profile — SeatMap/Retrieve needs a populated
    # travelers[] in the body, or it 500s with NullReferenceException.
    prof_resp = await _authed_get("/xapi/myunited/User/profile")
    prof = ((prof_resp.get("json") or {}).get("data") or {}).get("profile") or {}
    t0 = (prof.get("Travelers") or [{}])[0]
    from datetime import datetime, date
    dob_raw = t0.get("BirthDate") or ""
    try:
        dob_dt = datetime.fromisoformat(dob_raw.replace("Z","+00:00"))
        dob_str = dob_dt.strftime("%m/%d/%Y")
        age = (date.today().year - dob_dt.year) - (
            (date.today().month, date.today().day) < (dob_dt.month, dob_dt.day)
        )
    except Exception:
        dob_str = dob_raw[:10]
        age = None

    session_key = f"{cart_id}{uuid.uuid4()}"
    body = {
        "cartId": cart_id,
        "channelTransactionId": str(uuid.uuid4()),
        "reservationReferenceId": cart_id,
        "correlationId": "",
        "sessionKey": session_key,
        "dodCabins": dod_cabins,
        "seatMapRequest": {
            "recordLocator": None,
            "recordLocatorCreatedDate": None,
            "languageCode": "en-US",
            "isLapChild": False,
            "isAwardReservation": False,
            "flightSegments": [{
                "premiumProducts": [],
                "arrivalAirport": {"iataCode": destination, "iataCountryCode": {"CountryCode": "US"}},
                "arrivalDateTime": arrival_datetime,
                "checkInSegment": False,
                "classOfService": class_of_service,
                "coupons": [{}],
                "departureAirport": {"iataCode": origin, "iataCountryCode": {"CountryCode": "US"}},
                "departureDateTime": departure_datetime,
                "farebasisCode": fare_basis_code,
                "flightNumber": int(flight_number),
                "isValidSegment": True,
                "marketingAirlineCode": "UA",
                "operatingAirlineCode": "UA",
                "operatingFlightNumber": int(flight_number),
                "pricing": "true",
                "segmentNumber": int(segment_number),
            }],
            "lofSegments": [],
            "reservationReferences": None,
            "travelers": [{
                "specialServiceRequests": None,
                "lastName": t0.get("LastName"),
                "firstName": t0.get("FirstName"),
                "gender": t0.get("GenderCode") or "M",
                "passengerTypeCode": "ADT",
                "travelerIndex": "1.1",
                "loyaltyProfiles": [{
                    "loyaltyLevel": "0",
                    "loyaltyProgramCarrierCode": "UA",
                    "memberShipId": t0.get("MileagePlusId"),
                    "programId": "UA",
                }],
                "suffix": t0.get("Suffix") or "",
                "dateOfBirth": dob_str,
                "type": "ADT",
                "id": 1,
                "age": age,
            }],
            "bookingCode": class_of_service,
            "productCode": "ELF",        # "ELF" = Basic Economy; for other fares this may differ
            "callRtd": False,
            "dutyCode": None,
            "bundleCode": None,
            "channelId": "101",
            "channelName": "OBE",        # Online Booking Engine
            "isPetInCabin": False,
            "hasSSR": False,
            "pointOfSale": "US",
            "currencyCode": "USD",
        },
    }

    # Warm up the cart session — loading the cart populates server-side
    # state that SeatMap/Retrieve relies on. Without this, SeatMap returns
    # a NullReferenceException from UAL.ECommerce.Domain.SeatMap.
    await _authed_get(
        f"/api/ShoppingCart/LoadReservationAndCart"
        f"?cartId={cart_id}&workFlowType=1&clearBundles=false&clearSeats=false&isConfirmationPage=false"
    )

    resp = await _authed_post("/api/SeatMap/Retrieve", body=body)
    if resp.get("status") != 200:
        raise RuntimeError(
            f"SeatMap/Retrieve failed: status={resp.get('status')} "
            f"body={(resp.get('body') or '')[:300]}"
        )
    data = resp.get("json") or {}
    fi = data.get("flightInfo") or {}
    ai = data.get("aircraftInfo") or {}

    # Tier price lookup (strip out the per-seat validator tokens — too bulky)
    tiers_clean = []
    for t in (data.get("tiers") or []):
        pricing = t.get("pricing") or []
        first = pricing[0] if pricing else {}
        tiers_clean.append({
            "id": t.get("id"),
            "currencyCode": t.get("currencyCode"),
            "price": first.get("totalPrice"),
            "basePrice": first.get("basePrice"),
            "eligibility": first.get("eligibility"),
        })

    # Cabins — keep rows/seats/monumentRows but drop opaque `pricingValidators`
    cabins_clean = []
    total_avail = 0
    total_seats = 0
    has_exit = False
    has_free = False
    has_paid = False
    for cabin in (data.get("cabins") or []):
        cleaned = {
            k: v for k, v in cabin.items()
            if k in ("isUpperDeck","cabinType","cabinBrand","cabinBranded","layout",
                     "rowCount","columnCount","availableSeats","totalSeats",
                     "rows","monumentRows","adjacentSeats")
        }
        total_avail += cabin.get("availableSeats", 0) or 0
        total_seats += cabin.get("totalSeats", 0) or 0
        # Walk rows for summary flags + tier lookup
        for row in cleaned.get("rows") or []:
            for s in row.get("seats") or []:
                if s.get("isExit"): has_exit = True
                if s.get("isAvailable"):
                    tier_id = int(s.get("tier") or 0)
                    tier = next((t for t in tiers_clean if t["id"] == tier_id), None)
                    price = (tier or {}).get("price") or 0
                    if price > 0: has_paid = True
                    else: has_free = True
        cabins_clean.append(cleaned)

    # Heuristic: if no cabin had anything available AND all tiers say
    # "Seat selection not eligible for ELF Fare", it's Basic Economy locked.
    eligibility_msgs = " ".join((t.get("eligibility") or "") for t in tiers_clean)
    basic_locked = ("ELF" in eligibility_msgs and total_avail == 0)

    sm_id = f"united-seatmap:{cart_id}:{fi.get('marketingFlightNumber')}"
    return {
        "id": sm_id,
        "flightNumber": f"UA {fi.get('marketingFlightNumber','')}",
        "origin": fi.get("departureAirport"),
        "destination": fi.get("arrivalAirport"),
        "departureTime": fi.get("departureDate"),
        "fareBasisCode": fare_basis_code,
        "classOfService": class_of_service,
        "aircraftCode": ai.get("icr"),
        "totalSeats": total_seats,
        "availableSeats": total_avail,
        "cabins": cabins_clean,
        "tiers": tiers_clean,
        "hasExitRow": has_exit,
        "hasFreeSeats": has_free,
        "hasPaidSeats": has_paid,
        "basicEconomyLocked": basic_locked,
        "at": _UNITED_ORG,
        # Non-shape — useful for re-calling with same keys
        "_cartId": cart_id,
    }
