"""
United Airlines skill — profile, MileagePlus balances, reservations,
flight search, and travel history.

Auth mechanics (see requirements.md for the full story):
- Session cookies live on `.united.com` (the engine's cookie providers
  pull them from Brave). Critical cookies: AuthCookie, Session, User,
  PIM-SESSION-ID, 1pc_session, _ucid, plus Akamai bot-manager cookies
  (_abck, bm_*, ak_bmsc, akacd_*) which must pass through unchanged.
- Every API call wants `X-Authorization-api: bearer <hash>`. The bearer
  is minted by GET /api/auth/anonymous-token — misleading name; with
  cookies present it returns a USER-SCOPED token. Short-lived (~30min).

The skill mints a bearer per tool call (tiny cost — one extra request)
so each call is self-contained. A future optimization is caching on the
connection, but that requires a bearer-refresh path and isn't worth it
until we're making many calls per session.
"""

import json as _json

from agentos import client, connection, returns, timeout


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
                "_cartId": cart_id,
                "_parentProductId": parent_id,
            })
            for np in product.get("nestedProducts") or []:
                _emit(np, parent_id=product_id)

        for p in flight.get("products") or []:
            _emit(p)

    return offers
