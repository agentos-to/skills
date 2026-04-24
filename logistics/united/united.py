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


# Aircraft manufacturer lookup — keyed on IATA 3-char equipment code.
# ONLY populate manufacturer for equipment codes whose manufacturer is
# unambiguous and publicly known. Never fabricate — if the code isn't
# in this table, leave manufacturer null and let the graph fill in
# later from a canonical registry (ICAO Doc 8643).
#
# Source for each mapping: the EquipmentDescription string United
# itself returns in the search / cart responses ("Boeing 737-800",
# "Airbus A320"). We read the brand prefix from United's own data,
# then emit a proper `organization` node.
_AIRCRAFT_MANUFACTURERS = {
    "BOEING": {
        "shape": "organization",
        "name": "Boeing",
        "url": "https://www.boeing.com",
        "country": "US",
    },
    "AIRBUS": {
        "shape": "organization",
        "name": "Airbus",
        "url": "https://www.airbus.com",
        "country": "NL",  # Airbus SE is headquartered in Leiden, NL
    },
    "EMBRAER": {
        "shape": "organization",
        "name": "Embraer",
        "url": "https://embraer.com",
        "country": "BR",
    },
    "BOMBARDIER": {
        "shape": "organization",
        "name": "Bombardier",
        "url": "https://www.bombardier.com",
        "country": "CA",
    },
    "MITSUBISHI": {
        "shape": "organization",
        "name": "Mitsubishi Heavy Industries",
        "url": "https://www.mhi.com",
        "country": "JP",
    },
    "ATR": {
        "shape": "organization",
        "name": "ATR",
        "url": "https://www.atr-aircraft.com",
        "country": "FR",
    },
}


# IATA 3-char → ICAO 4-char equipment code. Populated from what United
# actually returns; extend as we see new codes. Never guess — if we
# haven't observed the mapping, leave icaoCode null.
_IATA_TO_ICAO_EQUIPMENT = {
    "738": "B738",   # Boeing 737-800
    "739": "B739",   # Boeing 737-900
    "7M8": "B38M",   # Boeing 737 MAX 8
    "7M9": "B39M",   # Boeing 737 MAX 9
    "752": "B752",   # Boeing 757-200
    "753": "B753",   # Boeing 757-300
    "763": "B763",   # Boeing 767-300
    "764": "B764",   # Boeing 767-400
    "772": "B772",   # Boeing 777-200
    "77W": "B77W",   # Boeing 777-300ER
    "788": "B788",   # Boeing 787-8
    "789": "B789",   # Boeing 787-9
    "78X": "B78X",   # Boeing 787-10
    "319": "A319",   # Airbus A319
    "320": "A320",   # Airbus A320
    "321": "A321",   # Airbus A321
    "32N": "A20N",   # Airbus A320neo
    "32Q": "A21N",   # Airbus A321neo
    "E75": "E75L",   # Embraer 175
    "CR7": "CRJ7",   # CRJ-700 (Bombardier; now De Havilland Canada for CRJs pre-2020)
    "CR9": "CRJ9",   # CRJ-900
}


def _aircraft_node(equipment_type: str | None, equipment_description: str | None) -> dict | None:
    """Build a proper aircraft node from United's equipment fields.

    - equipment_type: IATA 3-char code ("738", "7M8", "320") from
      United's EquipmentType field.
    - equipment_description: human-readable string ("Boeing 737-800",
      "Airbus A320") from United's EquipmentDescription field.

    Returns None if there isn't enough data to form a meaningful
    aircraft node (neither code nor description). We never fabricate
    the manufacturer; it's derived from the first word of the
    description. Manufacturer/model splitting is purely string-parsing
    on what United already gave us.
    """
    if not (equipment_type or equipment_description):
        return None

    iata = (equipment_type or "").strip() or None
    icao = _IATA_TO_ICAO_EQUIPMENT.get(iata) if iata else None
    desc = (equipment_description or "").strip()

    manufacturer = None
    model = desc or None
    variant = None
    if desc:
        # Parse "Boeing 737-800" → manufacturer=Boeing, model=737-800.
        # If the first token is a known manufacturer, split it out.
        parts = desc.split(None, 1)
        if len(parts) == 2 and parts[0].upper() in _AIRCRAFT_MANUFACTURERS:
            manufacturer = _AIRCRAFT_MANUFACTURERS[parts[0].upper()]
            model = parts[1]

    node: dict = {
        "shape": "aircraft",
    }
    if iata:
        node["iataCode"] = iata
    if icao:
        node["icaoCode"] = icao
    if model:
        node["model"] = model
    if variant:
        node["variant"] = variant
    if manufacturer:
        node["manufacturer"] = manufacturer
    return node


def _flight_from_displayflight(f: dict, marketing: str = "UA") -> dict:
    """Build a flight node from a DisplayFlight / DisplayTrips[].Flights[]
    entry — the PascalCase shape returned post-selection (RegisterFlights
    / LoadReservationAndCart / RegisterTravelers).

    United's DisplayFlight includes OriginDescription, OriginStateCode,
    OriginCountryCode, DestinationTimezoneOffset, and EquipmentDisclosures,
    so we can populate a fully-fledged flight node without a second
    lookup.
    """
    flight_num = f.get("FlightNumber") or ""
    eq = f.get("EquipmentDisclosures") or {}
    return {
        "id": f"{marketing}{flight_num}:{f.get('DepartDateTime','')}",
        "name": f"{marketing} {flight_num}",
        "flightNumber": f"{marketing} {flight_num}",
        "departureTime": _iso_depart(f.get("DepartDateTime"), f.get("OrgTimezoneOffset") or f.get("OriginTimezoneOffset")),
        "arrivalTime": _iso_depart(
            f.get("ArrivalDateTime") or f.get("DestinationDateTime"),
            f.get("DestTimezoneOffset") or f.get("DestinationTimezoneOffset"),
        ),
        "durationMinutes": f.get("TravelMinutesTotal") or f.get("TravelMinutes"),
        "cabinClass": (f.get("CabinType") or "").lower() or None,
        "airline": _UNITED_ORG if marketing == "UA" else {
            "shape": "airline", "iataCode": marketing,
            "name": f.get("MarketingCarrierDescription") or marketing,
        },
        "departsFrom": _airport_node_from_ua(
            iata=f.get("Origin"),
            name=f.get("OriginDescription"),
            country_code=f.get("OriginCountryCode"),
            state_code=f.get("OriginStateCode"),
            timezone_offset=f.get("OriginTimezoneOffset") or f.get("OrgTimezoneOffset"),
        ) or {"iataCode": f.get("Origin"), "name": f.get("Origin")},
        "arrivesAt": _airport_node_from_ua(
            iata=f.get("Destination"),
            name=f.get("DestinationDescription"),
            country_code=f.get("DestinationCountryCode"),
            state_code=f.get("DestinationStateCode"),
            timezone_offset=f.get("DestinationTimezoneOffset") or f.get("DestTimezoneOffset"),
        ) or {"iataCode": f.get("Destination"), "name": f.get("Destination")},
        "aircraft": _aircraft_node(
            eq.get("EquipmentType"),
            eq.get("EquipmentDescription"),
        ),
        "_originTerminal": f.get("OriginTerminal"),
        "_destinationTerminal": f.get("DestinationTerminal"),
        "_fareBasisCode": f.get("FareBasisCode"),
    }


def _airport_node_from_ua(
    iata: str | None,
    name: str | None = None,
    country_code: str | None = None,
    state_code: str | None = None,
    timezone_offset: int | None = None,
) -> dict:
    """Build a proper airport node from United's airport fields.

    United's airport records come in a few forms:
    - Full: {IATACode, Name: "Austin, TX, US (AUS)", IATACountryCode:
      {CountryCode: "US"}, StateProvince: {StateProvinceCode: "TX"}}
    - Flat (on flight segments): {Origin: "AUS", OriginDescription:
      "Austin, TX, US (AUS)", OriginCountryCode: "US", OriginStateCode:
      "TX", OriginTimezoneOffset: -5}

    This helper takes the already-parsed fields (caller does the flat/
    nested disambiguation). We extract the city by stripping the
    trailing " (XXX)" and the state/country suffix from `name`.

    Never fabricates — if a field isn't given, it isn't set on the node.
    """
    if not iata:
        return None
    node: dict = {
        "shape": "airport",
        "iataCode": iata,
        "name": name or iata,
    }
    # Parse "Austin, TX, US (AUS)" -> city="Austin". The Name suffix
    # " (IATA)" is always there; the state/country portion varies by
    # region (US: "City, ST, US", international: "City, CC"). We peel
    # from the left.
    if name:
        base = name.rsplit(" (", 1)[0]  # "Austin, TX, US"
        first_comma = base.split(",", 1)
        if first_comma:
            city = first_comma[0].strip()
            if city and city != iata:
                node["city"] = city
    if country_code:
        node["countryCode"] = country_code
    if state_code:
        node["region"] = state_code
    if timezone_offset is not None:
        # Keep the raw offset in hours from UTC. Not strictly a shape
        # field (the shape prefers IANA timezone strings), but useful
        # as a fallback until we resolve to IANA.
        node["_timezoneOffsetHours"] = timezone_offset
    return node


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


# ── cart inspection ─────────────────────────────────────────────────────────


@returns("reservation")
@connection("web")
@timeout(30)
async def get_cart(*, cart_id: str, **params) -> dict:
    """Read the current state of an in-progress booking cart.

    Hits `GET /api/ShoppingCart/LoadReservationAndCart?cartId=<UUID>` —
    the same call the SPA fires on every booking-flow page transition.
    Safe to call at any point between selection and checkout; it's
    read-only.

    Returned shape is a `reservation` stub similar to `select_flight`'s
    output: status=`hold`, total + currency, `trips[]` with `legs[]`.
    The raw cart blob (carrying SearchType, SelectedProducts, traveler
    state, seats, bundles, etc.) is attached as `_raw` for callers that
    want to probe fields the skill doesn't yet surface.

    Args:
        cart_id: UUID from search or select_flight output.
    """
    # These query params are what the SPA sends — LoadReservationAndCart
    # without them returns a generic "connection issues" error (observed
    # on freshly-created carts). Matching the SPA's URL shape succeeds.
    resp = await _authed_get(
        f"/api/ShoppingCart/LoadReservationAndCart?cartId={cart_id}"
        f"&workFlowType=1&clearBundles=false&clearSeats=false&isConfirmationPage=false",
    )
    if resp.get("status") != 200:
        raise RuntimeError(
            f"LoadReservationAndCart failed: status={resp.get('status')} "
            f"body={(resp.get('body') or '')[:300]}"
        )
    # Response envelope is Data.CartData.DisplayCart (LoadReservationAndCart)
    # or Data.DisplayCart (RegisterFlights/RegisterTravelers). Accept both.
    data = ((resp.get("json") or {}).get("Data") or {})
    cart_data = data.get("CartData") or data
    dc = cart_data.get("DisplayCart") or cart_data.get("Cart") or {}
    trips_raw = dc.get("DisplayTrips") or dc.get("Trips") or []

    trips: list[dict] = []
    for dt in trips_raw:
        flights = dt.get("Flights") or dt.get("DisplayFlights") or []
        legs = [_flight_from_displayflight(f) for f in flights]
        first = flights[0] if flights else {}
        last  = flights[-1] if flights else {}
        trips.append({
            "id": f"ua-trip:{cart_id}:{dt.get('TripIndex') or len(trips)+1}",
            "name": f"{dt.get('Origin') or first.get('Origin','?')}→{dt.get('Destination') or last.get('Destination','?')}",
            "tripType": "flight",
            "status": "held",
            "departureTime": _iso_depart(first.get("DepartDateTime"), first.get("OrgTimezoneOffset") or first.get("OriginTimezoneOffset")),
            "arrivalTime":   _iso_depart(
                last.get("ArrivalDateTime") or last.get("DestinationDateTime"),
                last.get("DestTimezoneOffset") or last.get("DestinationTimezoneOffset"),
            ),
            "carrier": _UNITED_ORG,
            "origin": _airport_node_from_ua(
                iata=dt.get("Origin") or first.get("Origin"),
                name=first.get("OriginDescription"),
                country_code=first.get("OriginCountryCode"),
                state_code=first.get("OriginStateCode"),
                timezone_offset=first.get("OriginTimezoneOffset") or first.get("OrgTimezoneOffset"),
            ) or {"iataCode": dt.get("Origin"), "name": dt.get("Origin")},
            "destination": _airport_node_from_ua(
                iata=dt.get("Destination") or last.get("Destination"),
                name=last.get("DestinationDescription"),
                country_code=last.get("DestinationCountryCode"),
                state_code=last.get("DestinationStateCode"),
                timezone_offset=last.get("DestinationTimezoneOffset") or last.get("DestTimezoneOffset"),
            ) or {"iataCode": dt.get("Destination"), "name": dt.get("Destination")},
            "legs": legs,
            "_tripIndex": dt.get("TripIndex"),
            "_bbxHash": first.get("BBXHash"),
            "_bbxSolutionSetId": first.get("BBXSolutionSetId"),
        })

    return {
        "id": f"united-cart:{cart_id}",
        "reservationType": "flight",
        "reservationId": cart_id,
        "status": "hold",
        "bookingType": "instant",
        "totalAmount": dc.get("GrandTotal"),
        "currency": "USD",
        "at": _UNITED_ORG,
        "trips": trips,
        "_cartId": cart_id,
        # SearchType 1 = one-way, 2 = round-trip (observed in captures).
        "_searchType": dc.get("SearchType"),
        "_tripCount": len(trips),
        "_cartRefId": cart_data.get("CartRefId"),
        "_raw": dc,
    }


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
    `connections[]`) into an agentOS `flight` shape.

    Uses the full airport + aircraft enrichment helpers so the flight
    node carries proper nested `departsFrom` / `arrivesAt` airport
    nodes (with city, countryCode, region) and a proper `aircraft`
    node (with model, manufacturer, IATA + ICAO codes).
    """
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
        "departsFrom": _airport_node_from_ua(
            iata=seg.get("origin"),
            name=seg.get("originDescription"),
            country_code=seg.get("originCountryCode"),
            state_code=seg.get("originStateCode"),
            timezone_offset=seg.get("orgTimezoneOffset"),
        ) or {"iataCode": seg.get("origin"), "name": seg.get("origin")},
        "arrivesAt": _airport_node_from_ua(
            iata=seg.get("destination"),
            name=seg.get("destinationDescription"),
            country_code=seg.get("destinationCountryCode"),
            state_code=seg.get("destinationStateCode"),
            timezone_offset=seg.get("destTimezoneOffset"),
        ) or {"iataCode": seg.get("destination"), "name": seg.get("destination")},
        "aircraft": _aircraft_node(
            equipment.get("equipmentType"),
            equipment.get("equipmentDescription"),
        ),
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
    cart_id: str = None,
    trip_index: int = 1,
    **params,
) -> list:
    """Search United flights. Returns an `offer[]` — one per fare bucket per
    flight option, with a nested `trips[]` relation whose `legs[]` are the
    individual flight segments.

    Round-trip is modeled as two calls: fire once for the outbound slice
    (default — `trip_index=1`, no `cart_id`), then again for the return
    slice (`trip_index=2`, passing the `_cartId` from any outbound offer).
    The second call sets `UsePassedCartId: true` so United reuses the
    same cart for the return slice. This mirrors the frontend round-trip
    UI which fires two FetchSSENestedFlights calls behind one idx=1→idx=2
    URL transition.

    `return_date` is currently only a hint for the URL deep-link; the
    body shape always carries a single-slice `Trips[]` — round-trip is
    caller-driven, not server-inferred.

    Args:
        origin: IATA code (e.g. 'AUS').
        destination: IATA code (e.g. 'SFO').
        depart_date: YYYY-MM-DD.
        return_date: Informational only — body is still single-slice.
        passengers: Count of adult passengers.
        cabin: economy, premium_economy, business, or first.
        include_basic: If True, surfaces Basic Economy as a fare option.
        award: If True, searches with MileagePlus miles pricing.
        cart_id: Optional cartId from a prior search — passing it flips
            UsePassedCartId to true and reuses that cart for this slice.
        trip_index: 1 for outbound, 2 for return. Sent as Trips[0].TripIndex.
    """
    cabin_pref = _CABIN_TO_PREF.get(cabin.lower(), "economy")
    fare_family = "ECONOMY" if cabin_pref == "economy" else cabin_pref.upper()

    # Return-slice on an existing round-trip cart uses a completely different
    # body shape (SearchTypeSelection: 3, empty Trips[]) — the server infers
    # the slice from the session's active cart. Captured from the SPA on
    # 2026-04-23. The slice-1 outbound uses type 1 with a full Trips[].
    is_return_slice = bool(cart_id) and int(trip_index) >= 2
    if is_return_slice:
        trips_list = []
        search_type_selection = 3
    else:
        trips_list = [{
            "Origin": origin.upper(),
            "Destination": destination.upper(),
            "DepartDate": depart_date,
            "Index": int(trip_index),
            "TripIndex": int(trip_index),
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
        }]
        search_type_selection = 1

    body = {
        "SearchTypeSelection": search_type_selection,
        "SortType": "bestmatches",
        "SortTypeDescending": False,
        "Trips": trips_list,
        "CabinPreferenceMain": cabin_pref,
        "PaxInfoList": [{"PaxType": 1} for _ in range(max(1, passengers))],
        "AwardTravel": bool(award),
        "NGRP": False,
        "CalendarLengthOfStay": 0,
        "PetCount": 0,
        "RecentSearchKey": "" if is_return_slice else f"{origin.upper()}{destination.upper()}{depart_date}",
        "CalendarFilters": {"Filters": {"PriceScheduleOptions": {"Stops": 1}}},
        "Characteristics": [
            {"Code": "SOFT_LOGGED_IN", "Value": False},
            {"Code": "UsePassedCartId", "Value": bool(cart_id)},
        ],
        "FareType": "Refundable",
        "BuildHashValue": "true",
        "EnableBasicPremiumProducts": bool(include_basic),
    }

    bearer = await _mint_bearer()
    if not bearer:
        raise RuntimeError("SESSION_EXPIRED: no bearer could be minted")

    # Prime the server-side session with round-trip context BEFORE the
    # actual flight search. The SPA fires this from the homepage when the
    # user picks dates; without it, the server decides SearchType from
    # other signals and lands on 1 (one-way). With it + the round-trip
    # Referer, the server knows this session's cart is round-trip.
    if return_date and trip_index == 1 and not cart_id:
        def _mmddyyyy(d: str) -> str:
            y, m, da = d.split("-")
            return f"{m}{da}{y}"
        try:
            await client.post(
                f"{_BASE}/api/FlexPricer/CalendarPricing",
                client="fetch",
                json={
                    "UserSelected": True,
                    "Depart": _mmddyyyy(depart_date),
                    "Return": _mmddyyyy(return_date),
                    "Origin": origin.upper(),
                    "Destination": destination.upper(),
                    "IsAward": bool(award),
                    "ClientCurrentDate": "",
                    "IsPremium": False,
                    "IsOneway": False,
                    "ExcludeBasicEconomy": not bool(include_basic),
                    "Travelers": {
                        "Adult": max(1, passengers),
                        "Senior": 0, "Infant": 0, "InfantOnLap": 0,
                        "Children01": 0, "Children02": 0, "Children03": 0, "Children04": 0,
                    },
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Referer": f"{_BASE}/en/us",
                    "X-Authorization-api": f"bearer {bearer}",
                },
            )
        except Exception:
            # Priming failure shouldn't block the actual search — worst case
            # we get a one-way cart and the caller retries.
            pass

    # Referer matters: the server scopes the cart's SearchType (1 = one-way,
    # 2 = round-trip) from the choose-flights URL the SPA *claims* to be on.
    # Body shape alone doesn't signal round-trip — only the Referer URL carrying
    # r=<return_date>&tt=1 does. Replicating that URL here lets us create a
    # round-trip cart from pure Python without driving the SPA.
    clm_code = "7" if cabin_pref == "economy" else "C"  # 7 = Economy incl Basic
    # sc takes one cabin-code per slice. For round-trip the SPA sends "7,7".
    # This is the key round-trip signal — presence of the comma is how the
    # server distinguishes the cart as multi-slice even though the body
    # carries a single Trips[] entry.
    sc_val = f"{clm_code},{clm_code}" if return_date else clm_code
    ref_params = [
        f"f={origin.upper()}",
        f"t={destination.upper()}",
        f"d={depart_date}",
    ]
    if return_date:
        ref_params.append(f"r={return_date}")
    ref_params += [
        f"sc={sc_val}",
        f"px={max(1, passengers)}",
        "taxng=1",
        "newHP=True",
        f"clm={clm_code}",
        "st=bestmatches",
        "tqp=R",
    ]
    referer = f"{_BASE}/en/us/fsr/choose-flights?{'&'.join(ref_params)}"

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
            "Referer": referer,
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
    trip_index: int = 1,
    origin: str = None,
    destination: str = None,
    depart_date: str = None,
    return_date: str = None,
    **params,
) -> dict:
    """Commit a flight selection — the step between search and traveler-info.

    Hits `/api/flight/RegisterFlights` with the cart id + booking token
    from `search_flights`. Returns a `reservation` stub with the cart
    state: total amount, fare breakdown, selected flight. The reservation
    isn't "booked" yet (no PNR) — it's a held cart, good for ~5 min
    before the checkout session idle-times out.

    For round-trip, call twice: first with trip_index=1 (outbound) and
    return_date set, then with trip_index=2 (return) on the same cart_id.
    Pass origin/destination/depart_date on both calls so the Referer the
    SPA would have sent is reproducible — the server keys SearchType (1
    vs 2) on that URL chain.

    Args:
        cart_id: UUID from the search meta event (carry over).
        booking_token: productId from the chosen offer (search result).
        flight_hash: hash from the chosen flight option (e.g. "118-1336-UA").
        fare_type: "Refundable" (default) or "NonRefundable".
        trip_index: 1 for outbound slice, 2 for return slice.
        origin, destination, depart_date, return_date: used to reconstruct
            the Referer URL so the server treats this as round-trip when
            return_date is present. All four are optional — without them
            the Referer is omitted and the cart ends up one-way.
    """
    referer = None
    if origin and destination and depart_date:
        sc_val = "7,7" if return_date else "7"
        ref_params = [
            f"f={origin.upper()}",
            f"t={destination.upper()}",
            f"d={depart_date}",
        ]
        if return_date:
            ref_params.append(f"r={return_date}")
        ref_params += [
            f"sc={sc_val}",
            "px=1",
            "taxng=1",
            "newHP=True",
            "clm=7",
            "st=bestmatches",
            "tqp=R",
        ]
        if trip_index == 2:
            ref_params.append(f"idx=2")
            ref_params.append(f"cartId={cart_id}")
        referer = f"{_BASE}/en/us/fsr/choose-flights?{'&'.join(ref_params)}"

    characteristics = [{"Code": "IsNewRTI", "Value": "true"}]
    if referer:
        # Frontend echoes the query string separately from the Referer header —
        # the server seems to use both. Everything after the ? in the referer.
        qpart = referer.split("?", 1)[1]
        characteristics.append({"Code": "fsrQueryParam", "Value": f"?{qpart}"})

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
        "Characteristics": characteristics,
    }
    extra_headers = {"Referer": referer} if referer else {}
    resp = await _authed_post("/api/flight/RegisterFlights", body=body, headers=extra_headers)
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
        legs = [_flight_from_displayflight(f) for f in flights]
        first = flights[0] if flights else {}
        last  = flights[-1] if flights else {}
        trips.append({
            "id": f"ua-trip:{cart_id}:{dt.get('TripIndex') or '1'}",
            "name": f"{dt.get('Origin') or first.get('Origin','?')}→{dt.get('Destination') or last.get('Destination','?')}",
            "tripType": "flight",
            "status": "held",
            "departureTime": _iso_depart(first.get("DepartDateTime"), first.get("OrgTimezoneOffset") or first.get("OriginTimezoneOffset")),
            "arrivalTime":   _iso_depart(
                last.get("ArrivalDateTime") or last.get("DestinationDateTime"),
                last.get("DestTimezoneOffset") or last.get("DestinationTimezoneOffset"),
            ),
            "carrier": _UNITED_ORG,
            "origin": _airport_node_from_ua(
                iata=dt.get("Origin") or first.get("Origin"),
                name=first.get("OriginDescription"),
                country_code=first.get("OriginCountryCode"),
                state_code=first.get("OriginStateCode"),
                timezone_offset=first.get("OriginTimezoneOffset") or first.get("OrgTimezoneOffset"),
            ) or {"iataCode": dt.get("Origin"), "name": dt.get("Origin")},
            "destination": _airport_node_from_ua(
                iata=dt.get("Destination") or last.get("Destination"),
                name=last.get("DestinationDescription"),
                country_code=last.get("DestinationCountryCode"),
                state_code=last.get("DestinationStateCode"),
                timezone_offset=last.get("DestinationTimezoneOffset") or last.get("DestTimezoneOffset"),
            ) or {"iataCode": dt.get("Destination"), "name": dt.get("Destination")},
            "legs": legs,
            "_tripIndex": dt.get("TripIndex"),
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


@returns("pass[]")
@connection("web")
@timeout(45)
async def register_seats(
    *,
    cart_id: str,
    seat_number: str,
    flight_number: int,
    origin: str,
    destination: str,
    departure_datetime: str,    # "2026-04-28T13:00"
    arrival_datetime: str,
    class_of_service: str = "N",
    fare_basis_code: str = "",
    traveler_index: str = "0",
    person_index: str = "1.1",
    **params,
) -> list:
    """Commit a seat selection — the step between seatmap and checkout.

    Hits `/api/ShoppingCart/RegisterSeats` with the chosen seat. The
    endpoint needs three things we look up from the live cart state:
    (a) the per-seat price validator token (from SeatMap/Retrieve),
    (b) the seat's SeatPromotionCode + SeatType (from SeatMap/Retrieve),
    (c) the full Reservation context (from LoadReservationAndCart).
    We fetch all three fresh and build the body.

    Returns a `pass[]` — one pass per assigned seat, keyed on
    (cart, seat, flight). The pass is in "held" state; it becomes
    "confirmed" after checkout.

    Args:
        cart_id: UUID from prior booking steps.
        seat_number: "22B" etc.
        flight_number, origin, destination, departure_datetime,
        arrival_datetime: flight identity (same as get_seatmap).
        class_of_service, fare_basis_code: fare identity.
        traveler_index: "0" for primary; increments for additional pax.
        person_index: "1.1" for primary (Reservation.Traveler.Person.Key).
    """
    # 1. Load cart for the Reservation context + PersonIndex
    cart_resp = await _authed_get(
        f"/api/ShoppingCart/LoadReservationAndCart"
        f"?cartId={cart_id}&workFlowType=1&clearBundles=false&clearSeats=false&isConfirmationPage=false"
    )
    if cart_resp.get("status") != 200:
        raise RuntimeError(f"LoadReservationAndCart failed: {cart_resp.get('status')}")
    cart_data = ((cart_resp.get("json") or {}).get("Data") or {}).get("CartData") or {}
    reservation_raw = cart_data.get("Reservation") or {}

    # 2. Get a fresh seatmap to pull the validator + promo code for our seat
    # (Reuse the same body-builder logic as get_seatmap)
    smap = await get_seatmap(
        cart_id=cart_id, flight_number=flight_number,
        origin=origin, destination=destination,
        departure_datetime=departure_datetime, arrival_datetime=arrival_datetime,
        class_of_service=class_of_service, fare_basis_code=fare_basis_code,
    )
    # Walk cabins.rows.seats to find seat_number, extract validator + price
    # But get_seatmap strips validators — so we need to re-fetch the raw SeatMap
    # response. Do it inline here (short — doesn't need a separate helper).
    import uuid
    from datetime import datetime, date

    prof_resp = await _authed_get("/xapi/myunited/User/profile")
    prof = ((prof_resp.get("json") or {}).get("data") or {}).get("profile") or {}
    t0 = (prof.get("Travelers") or [{}])[0]
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

    sm_body = {
        "cartId": cart_id,
        "channelTransactionId": str(uuid.uuid4()),
        "reservationReferenceId": cart_id,
        "correlationId": "",
        "sessionKey": f"{cart_id}{uuid.uuid4()}",
        "dodCabins": ["J", "O"],
        "seatMapRequest": {
            "recordLocator": None, "recordLocatorCreatedDate": None,
            "languageCode": "en-US", "isLapChild": False, "isAwardReservation": False,
            "flightSegments": [{
                "premiumProducts": [],
                "arrivalAirport": {"iataCode": destination, "iataCountryCode": {"CountryCode": "US"}},
                "arrivalDateTime": arrival_datetime, "checkInSegment": False,
                "classOfService": class_of_service, "coupons": [{}],
                "departureAirport": {"iataCode": origin, "iataCountryCode": {"CountryCode": "US"}},
                "departureDateTime": departure_datetime, "farebasisCode": fare_basis_code,
                "flightNumber": int(flight_number), "isValidSegment": True,
                "marketingAirlineCode": "UA", "operatingAirlineCode": "UA",
                "operatingFlightNumber": int(flight_number), "pricing": "true",
                "segmentNumber": 1,
            }],
            "lofSegments": [], "reservationReferences": None,
            "travelers": [{
                "specialServiceRequests": None,
                "lastName": t0.get("LastName"), "firstName": t0.get("FirstName"),
                "gender": t0.get("GenderCode") or "M",
                "passengerTypeCode": "ADT", "travelerIndex": "1.1",
                "loyaltyProfiles": [{
                    "loyaltyLevel": "0", "loyaltyProgramCarrierCode": "UA",
                    "memberShipId": t0.get("MileagePlusId"), "programId": "UA",
                }],
                "suffix": t0.get("Suffix") or "", "dateOfBirth": dob_str,
                "type": "ADT", "id": 1, "age": age,
            }],
            "bookingCode": class_of_service, "productCode": "ELF",
            "callRtd": False, "dutyCode": None, "bundleCode": None,
            "channelId": "101", "channelName": "OBE",
            "isPetInCabin": False, "hasSSR": False,
            "pointOfSale": "US", "currencyCode": "USD",
        },
    }
    sm_resp = await _authed_post("/api/SeatMap/Retrieve", body=sm_body)
    sm_data = sm_resp.get("json") or {}
    # Find the target seat + its per-traveler validator
    target_seat = None
    for cabin in sm_data.get("cabins") or []:
        for row in cabin.get("rows") or []:
            for s in row.get("seats") or []:
                if s.get("number") == seat_number:
                    target_seat = s; break
            if target_seat: break
        if target_seat: break
    if not target_seat:
        raise RuntimeError(f"Seat {seat_number} not found in seatmap")

    # Find validator — in tiers[].pricing[].pricingValidators[]
    tier_id = int(target_seat.get("tier") or 0)
    validator = None
    base_price = 0
    tax_amount = 0
    for t in sm_data.get("tiers") or []:
        if t.get("id") == tier_id:
            pricing = (t.get("pricing") or [])
            if pricing:
                pr = pricing[0]
                base_price = pr.get("basePrice") or 0
                tb = pr.get("taxBreakup") or []
                tax_amount = sum((tx.get("amount") or 0) for tx in tb)
                for pv in pr.get("pricingValidators") or []:
                    if pv.get("seatNumber") == seat_number:
                        validator = pv.get("amountValidator")
                        break
            break
    total_price = base_price + tax_amount

    # SeatPromotionCode + SeatType heuristic:
    # "PZA" (Preferred Zone Aisle) for paid premium economy seats,
    # "BEA" (Basic Economy?) for free economy. Not fully reverse-engineered yet,
    # so we echo the tier's promotion if present, else default by location.
    # TODO: when we have more captures, map this properly.
    seat_promo_code = target_seat.get("programPricingCode") or (
        "PZA" if target_seat.get("description") == "Preferred Zone" else ""
    )
    seat_type_str = target_seat.get("sellableSeatCategory") or "StandardPreferredZone"

    body = {
        "CartId": cart_id,
        "WorkFlowType": 1,
        "SeatAssignments": [{
            "DepartureAirportCode": origin,
            "ArrivalAirportCode": destination,
            "OriginalSegmentIndex": 1,
            "LegIndex": 0,
            "OriginalPrice": None,
            "SeatPrice": total_price,
            "Currency": "USD",
            "Seat": seat_number,
            "SeatPromotionCode": seat_promo_code,
            "PromotionalCouponCode": "",
            "ProductCode": "",
            "SeatType": seat_type_str,
            "TravelerIndex": str(traveler_index),
            "PersonIndex": person_index,
            "PCUSeat": False,
            "FlightNumber": int(flight_number),
            "SeatPriceValidator": validator,
            "UpgradeProductId": "",
            "FlattenedSeatIndex": 0,
            "DepartureDateTime": departure_datetime + (":00" if departure_datetime.count(":") < 2 else ""),
            "MoneyAmount": total_price,
            "MoneyCurrency": "USD",
            "ActualSeatPrice": 0,
            "InterlineSeatInfo": None,
            "oldSeatInfo": {"seatNumber": "", "purchasedFopType": ""},
            "OfferItemReferenceId": None,
            "SeatPricing": {
                "BaseAmount": base_price,
                "Currency": "USD",
                "OriginalBaseAmount": None,
                "OriginalTaxAmount": tax_amount,
                "SeatTaxes": [
                    {
                        "Amount": tax_amount,
                        "CurrencyCode": "USD",
                        "TaxCode": "US",
                        "TaxName": "U.S. Transportation Tax",
                        "TaxType": "FET",
                        "TaxPercentage": 7.5,
                        "TaxCurrencyDecimals": 2,
                    },
                ] if tax_amount else [],
            },
        }],
        "SeatsToRemove": [],
        "Characteristics": [
            {"Code": "OBE", "Value": "True"},
            {"Code": "IsNewSeatFlow", "Value": "True"},
        ],
        "Reservation": reservation_raw,
    }
    resp = await _authed_post("/api/ShoppingCart/RegisterSeats", body=body)
    if resp.get("status") != 200:
        raise RuntimeError(
            f"RegisterSeats failed: status={resp.get('status')} "
            f"body={(resp.get('body') or '')[:400]}"
        )

    # Build a pass[] result — one pass per seat assigned
    return [{
        "id": f"united-seat:{cart_id}:{flight_number}:{seat_number}",
        "name": f"UA {flight_number}  Seat {seat_number}",
        "status": "held",                       # → "confirmed" after checkout
        "ticketClass": class_of_service,
        "seatAssignment": seat_number,
        "at": _UNITED_ORG,
        "_cartId": cart_id,
        "_seatPrice": total_price,
    }]


@returns({"ascii": "string", "legend": "string"})
@connection("web")
@timeout(30)
async def render_seatmap(
    *,
    cart_id: str,
    flight_number: int,
    origin: str,
    destination: str,
    departure_datetime: str,
    arrival_datetime: str,
    class_of_service: str = "N",
    fare_basis_code: str = "",
    **params,
) -> dict:
    """Render an ASCII cabin chart for a flight. Calls get_seatmap under the
    hood and formats the output for display.

    Returns {"ascii": <multi-line string>, "legend": <legend string>}. The
    string has rows, seats (○ free, $ paid, ✕ occupied, █ blocked), aisle
    gaps between letter groups, and rule-lines for galleys / lavatories /
    exits.
    """
    sm = await get_seatmap(
        cart_id=cart_id, flight_number=flight_number,
        origin=origin, destination=destination,
        departure_datetime=departure_datetime, arrival_datetime=arrival_datetime,
        class_of_service=class_of_service, fare_basis_code=fare_basis_code,
    )

    tiers_map = {t["id"]: t for t in sm.get("tiers") or []}

    # Collect every cabin's rows + monument rows into ONE vertical sequence,
    # sorted by verticalGridNumber so First cabin stays forward of Economy
    # etc. We render a single continuous fuselage, with a thin band between
    # cabins to mark the section change.
    class_sections: list[dict] = []
    all_entries: list[tuple] = []  # (kind, vgn, payload, cabin_index)
    for ci, cabin in enumerate(sm.get("cabins") or []):
        class_sections.append({
            "index": ci,
            "brand": cabin.get("cabinBrand") or "",
            "layout": cabin.get("layout") or "",
            "available": cabin.get("availableSeats") or 0,
            "total": cabin.get("totalSeats") or 0,
        })
        for r in cabin.get("rows") or []:
            all_entries.append(("row", r.get("verticalGridNumber") or 0, r, ci))
        for m in cabin.get("monumentRows") or []:
            all_entries.append(("mon", m.get("verticalGridNumber") or 0, m, ci))
    all_entries.sort(key=lambda e: (e[3], e[1]))

    # Inner width the fuselage needs — must fit the widest row of any cabin.
    # Each seat is 3 chars wide (" X "), separated by spaces; an aisle is
    # a 3-wide gap ("   |   " style). We'll draw with a fixed column budget.
    #
    # For a 737: "ABC DEF" has 6 letter columns + 1 aisle = 7 columns.
    # Each seat cell = 3 chars; each seat gap = 1 char; aisle gap = 3 chars.
    # Row number sits in the aisle (3 chars). Extra label outside the right
    # fuselage wall shows "WING"/"EXIT" etc.
    max_columns = 0
    for cabin in sm.get("cabins") or []:
        cols = sum(len(g) for g in (cabin.get("layout") or "").split(" "))
        if cols > max_columns:
            max_columns = cols
    max_columns = max(max_columns, 6)  # min 6 for 3-3 layout

    # Inner-width budget: max_columns seat cells * 3 chars + (max_columns-1)
    # single-space gaps. For aisle we replace the gap with a 3-char row-number
    # slot. Worst case: "ABC DEF" → 6 seats + 5 gaps + 2 aisle-wide → that's
    # 6*3 + 5 + 2 = 25 chars inner. We just pick 25 and pad from there.
    INNER = 31  # gives breathing room for "║", " ", seats, row-number, seats, " ║"

    def _seat_cell(s: dict | None) -> str:
        if s is None:
            return "   "
        if s.get("itemType") == "MONUMENT":
            return "▓▓▓"
        if s.get("isBlocked") or s.get("isPermanentBlocked"):
            return "███"
        if s.get("isAvailable"):
            tier = int(s.get("tier") or 0)
            price = (tiers_map.get(tier) or {}).get("price") or 0
            if price > 0:
                # Show the price tier digit inside the seat so the user can
                # relate a seat to the price list. Fall back to $ if unknown.
                return f" ${tier}" if tier < 10 else " $ "
            return " ○ "
        return " ✕ "

    def _row_line(row: dict, layout: str) -> tuple[str, str]:
        """Return (interior, right_label) for a row. interior is the
        seats-plus-row-number body (without the fuselage walls). right_label
        is stuff to draw OUTSIDE the fuselage (WING / EXIT marker)."""
        groups = layout.split(" ")
        sl = {
            s.get("letter"): s
            for s in (row.get("seats") or [])
            if s.get("itemType") != "MONUMENT"
        }
        # Build each group of seats
        group_strs: list[str] = []
        for g in groups:
            parts = []
            for i, letter in enumerate(list(g)):
                parts.append(_seat_cell(sl.get(letter)))
            group_strs.append(" ".join(parts))

        # Join groups with the row-number slot in the middle aisle
        rn = str(row.get("number") or "")
        aisle_slot = f" {rn:^3} "
        interior = aisle_slot.join(group_strs)

        # Right-side label (outside the wall)
        labels = []
        if row.get("wing"):
            labels.append("wing")
        if any(s.get("isExit") for s in row.get("seats") or []):
            labels.append("exit-row")
        if any(s.get("isBulkhead") for s in row.get("seats") or []):
            labels.append("bulkhead")
        right_label = " ←  " + ", ".join(labels) if labels else ""
        return interior, right_label

    def _pad(inner: str) -> str:
        """Center `inner` within INNER chars."""
        return inner.center(INNER)

    # Build the drawing row-by-row.
    # Row body width (what sits inside the walls) = INNER. A seat row prints
    # as:  "│ " + padded_interior(INNER) + " │"  → total width INNER + 4.
    # With "│▐" / "▌│" on exit rows the outer width stays the same.
    TUBE_OUTER = INNER + 4    # "│ " + INNER + " │"
    lines: list[str] = []
    # Header above the plane
    lines.append(
        f"✈  {sm.get('flightNumber')}  "
        f"{sm.get('origin')} → {sm.get('destination')}  "
        f"{(sm.get('departureTime') or '').replace('T',' ')}  "
        f"aircraft {sm.get('aircraftCode')}"
    )
    lines.append("")

    # Top cap: a flat box. No nose/tail cone.
    lines.append("┌" + "─" * (TUBE_OUTER - 2) + "┐")
    # Cabin-brand banner row, centered inside the tube
    first_brand = class_sections[0]["brand"] if class_sections else ""
    lines.append("│ " + _pad(first_brand) + " │")
    # Column-letter header for the first cabin
    first_layout = class_sections[0]["layout"] if class_sections else ""
    if first_layout:
        groups = first_layout.split(" ")
        group_strs = []
        for g in groups:
            parts = [f" {letter} " for letter in list(g)]
            group_strs.append(" ".join(parts))
        header_inner = "     ".join(group_strs)   # wider aisle slot for the header
        lines.append("│ " + _pad(header_inner) + " │")
    lines.append("│ " + _pad("") + " │")

    # Walk the merged entries and draw row-by-row
    current_cabin = 0
    for kind, vgn, payload, cabin_idx in all_entries:
        # Cabin transition? Draw a cabin-divider band.
        if cabin_idx != current_cabin:
            lines.append("│ " + _pad("— — — — — — — — — — — —") + " │")
            new_brand = class_sections[cabin_idx]["brand"]
            lines.append("│ " + _pad(new_brand) + " │")
            new_layout = class_sections[cabin_idx]["layout"]
            if new_layout:
                groups = new_layout.split(" ")
                group_strs = []
                for g in groups:
                    parts = [f" {letter} " for letter in list(g)]
                    group_strs.append(" ".join(parts))
                header_inner = "     ".join(group_strs)
                lines.append("│ " + _pad(header_inner) + " │")
            lines.append("│ " + _pad("") + " │")
            current_cabin = cabin_idx

        if kind == "mon":
            monuments = payload.get("monuments") or []
            types = {m.get("itemType") for m in monuments}
            has_exit = any(m.get("isDoorExit") for m in monuments)
            if has_exit:
                # Draw a row with red exit markers OUTSIDE the fuselage wall
                lines.append("│▐" + _pad("═ ═ ═  DOOR/EXIT  ═ ═ ═") + "▌│")
            elif "LAV" in types:
                lines.append("│ " + _pad("⊟  LAVATORY  ⊟") + " │")
            elif "GALLEY" in types:
                lines.append("│ " + _pad("▒  GALLEY  ▒") + " │")
            continue

        # Seat row
        row = payload
        cabin = (sm.get("cabins") or [])[cabin_idx]
        interior, right_label = _row_line(row, cabin.get("layout") or "")
        padded = _pad(interior)
        # For exit-row rows, draw small red "▐" / "▌" on the wall edge
        is_exit_row = any(s.get("isExit") for s in row.get("seats") or [])
        left_wall = "│▐" if is_exit_row else "│ "
        right_wall = "▌│" if is_exit_row else " │"
        lines.append(left_wall + padded + right_wall + right_label)

    # Bottom cap: flat box.
    lines.append("└" + "─" * (TUBE_OUTER - 2) + "┘")

    # Legend
    legend_bits = [
        "○ free", "$N paid (tier N)", "✕ occupied",
        "█ blocked", "▓ monument",
    ]
    legend = "Legend:  " + "   ·   ".join(legend_bits)
    if sm.get("tiers"):
        legend += "\n\nPricing tiers:\n"
        for tid, t in sorted(tiers_map.items()):
            price = t.get("price") or 0
            if price > 0:
                legend += f"  tier {tid}: ${price:.2f}\n"
    if sm.get("basicEconomyLocked"):
        legend += "\n⚠ Basic Economy fare: seat selection is not purchasable on this ticket.\n"

    return {"ascii": "\n".join(lines), "legend": legend}


# ── booking confirmation gate ────────────────────────────────────────────────
#
# Two-step gate before we ever POST to /api/ShoppingCart/checkout. Agents can
# read prepare_booking freely; only confirm_booking charges a card, and it
# requires a signed blob from prepare_booking AND a human-echoed amount.
#
# Threat model: a misaligned agent that can call arbitrary tools should be
# unable to book a flight without the user's explicit confirmation. An HMAC
# blob prevents the agent from forging "I already asked and they said yes" —
# the blob can only be minted by reading the live cart state.
#
# The HMAC key is stored in the engine's skill_secret credential store so it
# persists across invocations but isn't readable by the agent directly.


def _booking_hmac_key() -> bytes:
    """Fetch (or mint) the booking-blob signing key.

    Tries the engine's skill_secret store first. If that's unavailable or
    doesn't persist across calls (observed 2026-04-23), falls back to a
    file in ~/.agentos/united-booking-key. Both paths produce the same
    value for the life of the key file — so prepare_booking and
    confirm_booking across separate skill invocations verify against the
    same signature.
    """
    import os, secrets, pathlib
    # Prefer skill_secret if it actually persists
    try:
        k = skill_secret.get("booking_hmac_key")
        if k:
            return k.encode("utf-8") if isinstance(k, str) else k
    except Exception:
        pass
    # File-backed fallback: ~/.agentos/united-booking-key
    key_path = pathlib.Path.home() / ".agentos" / "united-booking-key"
    if key_path.exists():
        return key_path.read_bytes().strip()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    new_key = secrets.token_hex(32).encode("utf-8")
    # Atomic write: write to tmp then rename, 0o600 perms
    tmp = key_path.with_suffix(".tmp")
    tmp.write_bytes(new_key)
    tmp.chmod(0o600)
    tmp.rename(key_path)
    try:
        skill_secret.set("booking_hmac_key", new_key.decode("utf-8"))
    except Exception:
        pass
    return new_key


def _sign_blob(payload: dict) -> str:
    """HMAC-SHA256 over the canonical JSON payload. Returns hex digest."""
    import hmac, hashlib, json as _j
    canonical = _j.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(_booking_hmac_key(), canonical, hashlib.sha256).hexdigest()


def _verify_blob(blob: dict) -> tuple[bool, str]:
    """Verify a blob's signature and non-expiry. Returns (ok, error_reason)."""
    import hmac, time
    sig = blob.get("_signature")
    payload = {k: v for k, v in blob.items() if k != "_signature"}
    if not sig:
        return False, "missing signature"
    expected = _sign_blob(payload)
    if not hmac.compare_digest(sig, expected):
        return False, "signature mismatch — blob was tampered with or minted with a different key"
    exp = payload.get("expires_at")
    if exp and time.time() > exp:
        return False, f"blob expired (expired {int(time.time() - exp)}s ago — re-run prepare_booking)"
    return True, ""


# --- booking_offer enrichment helpers ---------------------------------------
#
# These extract rich line items from United's cart responses and the
# /api/user/creditCards endpoint, then compose them into a booking_offer
# shape. Deterministic: only emit what the provider returned.


async def _fetch_saved_cards_for_user() -> list[dict]:
    """Fetch the logged-in user's saved cards from /api/user/creditCards.

    Returns the raw list under data.CreditCards, or [] on failure. No
    PAN is ever returned — only tokenized handles and last4 + BIN.
    """
    try:
        resp = await _authed_get("/api/user/creditCards")
        if resp.get("status") != 200:
            return []
        return ((resp.get("json") or {}).get("data") or {}).get("CreditCards") or []
    except Exception:
        return []


def _payment_method_from_card(card: dict) -> dict:
    """Build a payment_method node from a /api/user/creditCards row.

    Airlines return IATA 2-char codes in `Code` (AX/VI/MC/DS/DC/TP/JC/UP).
    We preserve those as `subtype` and normalize `type` to "card". All
    opaque handles (Key, PersistentToken, AccountNumberToken) go into
    `providerTokens` — never inspected by the agent, round-tripped into
    the checkout POST.
    """
    last4 = str(card.get("AccountNumberLastFourDigits") or "")
    brand = card.get("CCTypeDescription") or ""
    identifier = card.get("AccountNumberToken") or card.get("Key") or f"{brand}-{last4}"
    return {
        "shape": "payment_method",
        "identifier": identifier,
        "type": "card",
        "subtype": card.get("Code"),
        "brand": brand,
        "displayName": (card.get("AccountNumberMasked") or f"{brand} ****{last4}").strip(),
        "customDescription": card.get("CustomDescription"),
        "holderName": (card.get("Payor") or {}).get("GivenName"),
        "last4": last4 or None,
        "binRange": card.get("CreditCardBinRange"),
        "expMonth": card.get("ExpMonth"),
        "expYear": card.get("ExpYear"),
        "expirationDate": card.get("ExpirationDate"),
        "isDefault": bool(card.get("IsDefault")),
        "isPrimary": bool(card.get("IsPrimary")),
        "isSelected": bool(card.get("IsSelected")),
        "status": "active",
        "at": _UNITED_ORG,
        "providerTokens": {
            "persistentToken": card.get("PersistentToken"),
            "accountNumberToken": card.get("AccountNumberToken"),
            "key": card.get("Key"),
            "addressKey": card.get("AddressKey"),
        },
    }


def _fare_nodes_from_cart(dc: dict, trips: list[dict]) -> list[dict]:
    """Build fare nodes from DisplayTrips[].Flights[].FareBasisCode +
    DisplayPrices[].Amount. United doesn't itemize base-fare per leg in
    the cart response — it emits one pax-type total. So we emit one fare
    node per (unique fareBasisCode, pax-type), scoped to all legs it
    covers.
    """
    # Map passenger-type row → amount
    dp = (dc.get("DisplayPrices") or [{}])[0] if dc.get("DisplayPrices") else {}
    base_amount = dp.get("Amount")
    pax_type = dp.get("PaxTypeCode") or "ADT"
    currency = dp.get("Currency") or "USD"
    # Collect distinct fare basis codes per trip
    fares: list[dict] = []
    for trip in dc.get("DisplayTrips") or []:
        flights = trip.get("Flights") or []
        if not flights:
            continue
        # Pick the first segment's fare basis as the trip's fare (for
        # simple single-class itineraries); TODO extend to mixed-cabin.
        f0 = flights[0]
        fbc = f0.get("FareBasisCode")
        if not fbc:
            continue
        fares.append({
            "shape": "fare",
            "id": f"united-fare:{fbc}",
            "identifier": fbc,
            "bookingCode": (fbc or "")[:1] or None,
            "cabinClass": (f0.get("CabinType") or "").lower() or None,
            "fareFamily": None,   # not surfaced in DisplayCart; comes from search products
            "basePrice": base_amount,  # same base amount applies across segments for single-class
            "currency": currency,
            "passengerType": pax_type,
            "at": _UNITED_ORG,
            "for": {"iataCode": trip.get("Origin"), "name": f"{trip.get('Origin')}→{trip.get('Destination')}"},
        })
    return fares


def _tax_line_nodes_from_cart(dc: dict) -> list[dict]:
    """Extract itemized tax_line nodes from DisplayPrices[0].SubItems[].

    United lists every tax/fee individually — each SubItem has Description
    and Amount, and recurs per segment when applicable (e.g. U.S.
    Transportation Tax appears twice on a round-trip, once per leg).
    """
    taxes: list[dict] = []
    dp = (dc.get("DisplayPrices") or [{}])[0] if dc.get("DisplayPrices") else {}
    currency = dp.get("Currency") or "USD"
    for i, sub in enumerate(dp.get("SubItems") or []):
        desc = sub.get("Description") or sub.get("Code") or f"Tax {i}"
        # Infer a 2-char IATA-style code from the description's opening
        # word when possible — but don't fabricate codes the provider
        # didn't emit. Just use description as-is.
        taxes.append({
            "shape": "tax_line",
            "id": f"united-tax:{i}:{sub.get('Key', i)}",
            "code": sub.get("Code") or None,
            "description": desc,
            "amount": sub.get("Amount"),
            "currency": currency,
            "kind": "tax" if "tax" in desc.lower() else ("fee" if "fee" in desc.lower() or "charge" in desc.lower() else "tax"),
            "country": "US" if "U.S." in (desc or "") else None,
            "appliesToIndex": i,  # ordered by segment as United returned them
            "merchantImposed": False,
            "at": _UNITED_ORG,
        })
    return taxes


def _tax_breakdown_line(tl: dict) -> str:
    """Human-readable single line like 'U.S. Transportation Tax      $13.54'"""
    desc = tl.get("description", "?")
    amt = tl.get("amount") or 0
    return f"  {desc:<50} ${amt:>8.2f}"


@returns("booking_offer")
@connection("web")
@timeout(30)
async def prepare_booking(
    *,
    cart_id: str,
    signature_ttl_seconds: int = 300,
    **params,
) -> dict:
    """Produce a signed booking_offer node from the current cart state.

    This is the safe, read-only half of the booking gate. It fetches
    the live cart via LoadReservationAndCart, the user's saved cards
    via /api/user/creditCards, and composes a rich `booking_offer`
    node:

      - trips[] — enriched with airport city/state/country and aircraft
        manufacturer/model
      - fares[] — one per fare component, with fareBasisCode, class
      - taxLines[] — every tax/fee line itemized (US Transportation Tax,
        XF Passenger Facility Charge, AY Security Fee, etc.)
      - paymentMethod — the currently selected saved card (IsSelected=true
        from /api/user/creditCards), with last4, brand, expiry, and
        opaque providerTokens
      - totalAmount, baseAmount, taxAmount, currency
      - referenceNumber — the short human-readable cart ID United shows
        on screen (641457887)
      - cartId — the long UUID
      - HMAC-signed blob and expiresAt

    The signed `blob` (also available as `_signature` on the returned
    node) is required by confirm_booking to actually charge the card.
    The agent cannot forge a blob; only a live cart read can mint one.

    Args:
        cart_id: UUID of the cart to book.
        signature_ttl_seconds: how long the signature is valid (default
            300s / 5 min). Short TTL prevents stale-blob replay.
    """
    import time, base64, hashlib, json as _j

    cart = await get_cart(cart_id=cart_id)
    dc_raw = cart.get("_raw") or {}
    if not dc_raw or not cart.get("totalAmount"):
        raise RuntimeError(
            f"Cart {cart_id} is empty or unreadable — cannot prepare a booking. "
            f"Make sure both legs are selected and register_traveler has been called."
        )

    total = float(cart.get("totalAmount"))
    currency = cart.get("currency") or "USD"
    dp0 = (dc_raw.get("DisplayPrices") or [{}])[0] if dc_raw.get("DisplayPrices") else {}
    base_amount = dp0.get("Amount")
    tax_lines = _tax_line_nodes_from_cart(dc_raw)
    tax_total = sum((tl.get("amount") or 0) for tl in tax_lines)
    fares = _fare_nodes_from_cart(dc_raw, cart.get("trips") or [])

    # Payment method: which card is "IsSelected" on the user's account.
    # If no card is marked selected, pick the default, then the first.
    saved_cards = await _fetch_saved_cards_for_user()
    selected_card = next((c for c in saved_cards if c.get("IsSelected")), None)
    if not selected_card:
        selected_card = next((c for c in saved_cards if c.get("IsDefault")), None)
    if not selected_card and saved_cards:
        selected_card = saved_cards[0]
    payment_method = _payment_method_from_card(selected_card) if selected_card else None
    all_payment_methods = [_payment_method_from_card(c) for c in saved_cards]

    # Billing address — for now we surface the label string from the
    # card record. The full structured address lives in /api/user/addresses
    # which we don't fetch here to keep this read lean; deferred.
    billing_address = None  # TODO: fetch /api/user/addresses and pick the one matching selected_card.AddressKey

    # Travelers
    travelers = dc_raw.get("DisplayTravelers") or []

    # Timing
    now = int(time.time())
    expires_at = now + int(signature_ttl_seconds)

    # Itinerary hash — stable, deterministic fingerprint of what would
    # be committed. Canonical JSON, sorted keys, so nothing moves under
    # the signature.
    itin = {
        "cart_id": cart_id,
        "trips": [
            {
                "origin": (t.get("origin") or {}).get("iataCode"),
                "destination": (t.get("destination") or {}).get("iataCode"),
                "departure": t.get("departureTime"),
                "arrival": t.get("arrivalTime"),
                "flights": [l.get("flightNumber") for l in (t.get("legs") or [])],
            }
            for t in (cart.get("trips") or [])
        ],
        "total": total,
        "currency": currency,
        "payment_last4": (payment_method or {}).get("last4"),
    }
    itin_canonical = _j.dumps(itin, sort_keys=True, separators=(",", ":")).encode("utf-8")
    itinerary_hash = "sha256:" + hashlib.sha256(itin_canonical).hexdigest()

    # Signed payload — everything bound by the signature
    signed_payload = {
        "version": 2,
        "cart_id": cart_id,
        "reference_number": cart.get("_cartRefId"),
        "total_amount": total,
        "currency": currency,
        "itinerary_hash": itinerary_hash,
        "payment_method_last4": (payment_method or {}).get("last4"),
        "payment_method_identifier": (payment_method or {}).get("identifier"),
        "search_type": dc_raw.get("SearchType"),
        "prepared_at": now,
        "expires_at": expires_at,
    }
    signature = _sign_blob(signed_payload)
    blob = {**signed_payload, "_signature": signature}
    blob_str = base64.b64encode(_j.dumps(blob).encode("utf-8")).decode("ascii")

    amount_str = f"{currency} {total:.2f}"

    # Human-readable itinerary summary
    itin_lines: list[str] = []
    for t in cart.get("trips") or []:
        for l in t.get("legs") or []:
            ac = l.get("aircraft") or {}
            ac_model = (ac.get("model") or "").strip()
            manu = (ac.get("manufacturer") or {}).get("name")
            ac_str = f"{manu} {ac_model}".strip() if manu else ac_model
            itin_lines.append(
                f"{l.get('flightNumber','?').strip()}  "
                f"{(l.get('departsFrom') or {}).get('city') or (l.get('departsFrom') or {}).get('iataCode','?')} "
                f"→ {(l.get('arrivesAt') or {}).get('city') or (l.get('arrivesAt') or {}).get('iataCode','?')}  "
                f"{(l.get('departureTime') or '')[:16].replace('T',' ')}  "
                f"({ac_str})"
            )

    review_text = (
        f"\nBOOKING REVIEW — cart #{cart.get('_cartRefId') or cart_id[:8]}\n"
        + "=" * 60 + "\n"
        + "Itinerary:\n"
        + "\n".join("  " + l for l in itin_lines)
        + "\n\n"
        + f"Base fare:      ${base_amount:>8.2f} {currency}\n"
        + "Taxes & fees:\n"
        + "\n".join(_tax_breakdown_line(tl) for tl in tax_lines)
        + f"\n  {'TOTAL':<50} ${total:>8.2f}\n\n"
    )
    if payment_method:
        review_text += (
            f"Charge to:      {payment_method['displayName']}"
            + (f" ({payment_method['customDescription']})" if payment_method.get('customDescription') else "")
            + f"  exp {payment_method.get('expirationDate','?')}\n"
        )
    review_text += f"\nExpires in {int(signature_ttl_seconds/60)} min. To proceed, call:\n"
    review_text += f'  confirm_booking(blob=<above>, confirm_amount="{amount_str}", payment_method_last4="{(payment_method or {}).get("last4","????")}", dry_run=False)\n'

    # Build the booking_offer node
    offer = {
        "shape": "booking_offer",
        "id": f"united-booking-offer:{cart_id}",
        "cartId": cart_id,
        "referenceNumber": cart.get("_cartRefId"),
        "status": "ready",
        "preparedAt": now,
        "expiresAt": expires_at,
        "currency": currency,
        "baseAmount": base_amount,
        "taxAmount": round(tax_total, 2),
        "totalAmount": total,
        "itineraryHash": itinerary_hash,
        "signature": signature,
        "signatureAlg": "HS256",
        "signedBy": "self",
        "checkoutUrl": f"{_BASE}/en/us/book-flight/checkout/{cart_id}?tqp=R",
        "isRefundable": not bool(dc_raw.get("IsNonRefundable")),
        "isChangeable": not bool(dc_raw.get("IsNonChangeable")),
        "conditions": None,
        "at": _UNITED_ORG,
        # Relations populated as nested objects
        "trips": cart.get("trips") or [],
        "fares": fares,
        "taxLines": tax_lines,
        "paymentMethod": payment_method,
        "billingAddress": billing_address,
        "guests": [
            {
                "shape": "person",
                "dateOfBirth": t.get("DateOfBirth"),
                "type": t.get("PaxTypeDescription"),
            }
            for t in travelers
        ],
        # Non-shape extras
        "blob": blob_str,
        "review": review_text,
        "_availablePaymentMethods": all_payment_methods,
        "_searchType": dc_raw.get("SearchType"),
    }
    return offer


@returns({
    "status": "string",
    "pnr": "string",
    "cart_id": "string",
    "charged_amount": "number",
    "message": "string",
})
@connection("web")
@timeout(60)
async def confirm_booking(
    *,
    blob: str,
    confirm_amount: str,
    payment_method_last4: str,
    dry_run: bool = True,
    **params,
) -> dict:
    """Charge the card and book the flight — the only tool in the skill
    that moves money.

    ALL of the following must succeed or this tool refuses:

    1. Blob signature verifies (HMAC-SHA256 with the skill's secret key).
    2. Blob hasn't expired (5-minute TTL from prepare_booking).
    3. `confirm_amount` is an EXACT string match to the blob's total,
       formatted as "USD 464.36" (or equivalent currency). No substring
       matches, no rounding tolerance — the human must type the exact
       figure they saw.
    4. `payment_method_last4` matches a card currently on file in the
       cart's payment methods. Nothing else is accepted.
    5. `dry_run` must be explicitly set to False. Default is True.
       (Tools passing through model output often forget False defaults;
       this forces an intentional override.)

    If any check fails, raises RuntimeError with the specific reason and
    does not contact United's checkout endpoint.

    In dry_run mode, all checks run but no checkout POST is sent — useful
    for agents to validate inputs before the real call. Returns
    status=`dry_run_ok` on success.

    Args:
        blob: the base64-encoded signed blob from prepare_booking.
        confirm_amount: exact price string matching the blob total
            (e.g. "USD 464.36").
        payment_method_last4: last-4 of the card to charge, e.g. "1007"
            for the AMEX.
        dry_run: must be explicitly False to actually book.

    Returns:
        On real booking: status=`booked`, PNR, charged amount.
        On dry_run: status=`dry_run_ok`, summary.
    """
    import base64, json as _j
    # 1. Decode + verify blob
    try:
        decoded = _j.loads(base64.b64decode(blob.encode("ascii")).decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"confirm_booking: blob is malformed (decode failed): {e}")
    ok, err = _verify_blob(decoded)
    if not ok:
        raise RuntimeError(f"confirm_booking: blob verification FAILED — {err}")

    cart_id = decoded["cart_id"]
    blob_total = float(decoded["total_amount"])
    blob_currency = decoded.get("currency", "USD")

    # 2. Confirm-amount exact match
    expected = f"{blob_currency} {blob_total:.2f}"
    if (confirm_amount or "").strip() != expected:
        raise RuntimeError(
            f"confirm_booking: confirm_amount mismatch. "
            f"Expected exactly '{expected}', got '{confirm_amount}'. "
            f"This check exists so a human sees the real price before any charge."
        )

    # 3. Re-read the live cart and make sure the total HASN'T CHANGED since
    #    prepare_booking. Airlines occasionally reprice a cart before
    #    checkout; we refuse to charge a different amount than the user
    #    confirmed.
    live = await get_cart(cart_id=cart_id)
    live_total = live.get("totalAmount")
    if live_total is None:
        raise RuntimeError(
            f"confirm_booking: cart {cart_id} is no longer readable — "
            f"may have expired. Re-run prepare_booking to snapshot fresh."
        )
    if abs(float(live_total) - blob_total) > 0.01:
        raise RuntimeError(
            f"confirm_booking: cart total has CHANGED since prepare_booking. "
            f"Blob snapshot was {expected}; live cart is {blob_currency} {live_total:.2f}. "
            f"Refusing to charge. Re-run prepare_booking and reconfirm."
        )

    # 4. Validate payment method against what's on file.
    # Pull from /api/user/creditCards — the authoritative list of saved
    # cards on the user's account. PCI-safe: returns last4 + opaque
    # tokens only.
    saved_cards = await _fetch_saved_cards_for_user()
    last4_on_file = [str(c.get("AccountNumberLastFourDigits") or "") for c in saved_cards]
    last4_on_file = [s for s in last4_on_file if s]
    matched_card = next(
        (c for c in saved_cards if str(c.get("AccountNumberLastFourDigits") or "") == payment_method_last4),
        None,
    )
    if not matched_card:
        raise RuntimeError(
            f"confirm_booking: payment_method_last4={payment_method_last4} "
            f"does not match any card on file (saw: {last4_on_file or 'none'}). "
            f"Refusing to charge. To add a card, the user must do it in the "
            f"United website — this skill does not store new cards."
        )

    # 5. dry_run gate
    if dry_run is not False:
        card_desc = (
            matched_card.get("AccountNumberMasked")
            or f"{matched_card.get('CCTypeDescription','?')} ****{payment_method_last4}"
        )
        return {
            "status": "dry_run_ok",
            "pnr": "",
            "cart_id": cart_id,
            "charged_amount": 0.0,
            "message": (
                f"Dry run OK. Blob verified, amount matches ({expected}), "
                f"live cart total matches, payment card {card_desc} found on file. "
                f"Call again with dry_run=False to actually charge and book."
            ),
        }

    # ── ACTUAL CHECKOUT — all gates passed ─────────────────────────────────
    # The exact body shape for /api/ShoppingCart/checkout hasn't been
    # captured yet. Explicitly refuse rather than guess — guessing a
    # payment-submission body is the wrong kind of brave.
    raise RuntimeError(
        "confirm_booking: all gates passed but the /api/ShoppingCart/checkout "
        "endpoint body shape has not been reverse-engineered yet. The skill "
        "refuses to guess the body for a payment call. Next: capture a real "
        "checkout POST from the SPA (click Agree and purchase with Network "
        "intercept running) to learn the body shape, then wire it here."
    )

