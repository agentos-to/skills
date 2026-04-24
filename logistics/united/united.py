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


@returns({
    "given_name": "string",
    "middle_name": "string",
    "surname": "string",
    "date_of_birth": "string",
    "gender": "string",
    "mileage_plus": "string",
    "emails": "array",
    "phones": "array",
    "known_traveler_number": "string",
    "redress_number": "string",
    "primary_email": "string",
    "primary_phone": "string",
    "primary_phone_country_code": "string",
})
@connection("web")
@timeout(20)
async def get_contact_info(**params) -> dict:
    """Fetch the logged-in user's saved contact info (phones, emails, KTN,
    redress) and identity (name/DOB/gender) from their MileagePlus profile.

    Returns a plain dict — no shape — intended for booking flows that need
    to prefill traveler contact fields or to render a confirmation card.
    The four underlying endpoints are:
      - `/xapi/myunited/User/profile` — name, DOB, gender, MP#
      - `/api/user/phoneNumbers`      — all saved phones, flagged IsPrimary
      - `/api/user/emailAddresses`    — all saved emails, flagged IsPrimary
      - `/api/user/travelerSupplementaryTravelInfo` — KTN (Type=K), Redress (Type=R)
    """
    prof_resp = await _authed_get("/xapi/myunited/User/profile")
    prof = ((prof_resp.get("json") or {}).get("data") or {}).get("profile") or {}
    t0 = (prof.get("Travelers") or [{}])[0]

    dob_raw = t0.get("BirthDate") or ""
    dob = dob_raw
    if dob_raw:
        from datetime import datetime
        try:
            dob = datetime.fromisoformat(dob_raw.replace("Z", "+00:00")).strftime("%m/%d/%Y")
        except Exception:
            dob = dob_raw[:10]

    em_resp = await _authed_get("/api/user/emailAddresses")
    em_list = ((em_resp.get("json") or {}).get("data") or {}).get("EmailAddresses") or []
    ph_resp = await _authed_get("/api/user/phoneNumbers")
    ph_list = ((ph_resp.get("json") or {}).get("data") or {}).get("PhoneNumbers") or []
    sup_resp = await _authed_get("/api/user/travelerSupplementaryTravelInfo")
    sup = ((sup_resp.get("json") or {}).get("data") or {}).get("SupplementaryTravelInfos") or []

    def _pick_primary(xs, key):
        return next((x for x in xs if x.get("IsPrimary")), xs[0] if xs else {}).get(key)

    emails = [{
        "email": e.get("EmailAddress"),
        "type": e.get("EmailType") or e.get("TypeCode"),
        "primary": bool(e.get("IsPrimary")),
    } for e in em_list if e.get("EmailAddress")]
    def _combine_phone(p: dict) -> str:
        """United stores phones split into AreaNumber + PhoneNumber. Join them."""
        area = (p.get("AreaNumber") or "").strip()
        sub  = (p.get("PhoneNumber") or "").strip()
        return (area + sub) if area else sub

    phones = [{
        "number": _combine_phone(p),
        "area_number": p.get("AreaNumber"),
        "subscriber_number": p.get("PhoneNumber"),
        "extension": p.get("ExtensionNumber"),
        "country_code": p.get("CountryPhoneNumber") or "1",
        "country": p.get("CountryCode") or "US",
        "type": p.get("ChannelTypeDescription") or p.get("ChannelTypeCode") or p.get("Description"),
        "primary": bool(p.get("IsPrimary")),
    } for p in ph_list if p.get("PhoneNumber")]
    ktn = next((s.get("Number") for s in sup if (s.get("Type") or "").upper() == "K"), None)
    redress = next((s.get("Number") for s in sup if (s.get("Type") or "").upper() == "R"), None)

    return {
        "given_name":   t0.get("FirstName"),
        "middle_name":  t0.get("MiddleName"),
        "surname":      t0.get("LastName"),
        "date_of_birth": dob,
        "gender":       t0.get("GenderCode") or t0.get("Gender"),
        "mileage_plus": t0.get("MileagePlusId"),
        "emails":       emails,
        "phones":       phones,
        "known_traveler_number": ktn,
        "redress_number":        redress,
        "primary_email":  _pick_primary(em_list, "EmailAddress"),
        "primary_phone":  next((p["number"] for p in phones if p["primary"]), phones[0]["number"] if phones else None),
        "primary_phone_country_code": _pick_primary(ph_list, "CountryPhoneNumber") or "1",
    }


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


def _parse_mytrips_datetime(s: str | None) -> str | None:
    """Parse United's MyTrips datetime format (`M/D/YYYY h:mm:ss AM/PM`)
    into ISO 8601. Returns None for empty / unparseable.

    United's MyTrips endpoint uses a locale-dependent "4/28/2026 1:00:00 PM"
    string instead of the ISO format search_flights uses. Normalize so
    downstream consumers don't have to care.
    """
    if not s or not isinstance(s, str):
        return None
    from datetime import datetime
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).isoformat()
        except Exception:
            pass
    return s  # return as-is so we never lose the raw value


def _characteristic_value(chars: list[dict] | None, code: str) -> str | None:
    """Pull a Value from a Characteristic[] list matching a Code.

    United's reservation envelopes use a key/value pattern — the top-level
    `Characteristic: [{Code, Value}]` carries PNR-scoped flags like
    HAS_ETICKET, PNR_STATUS, PNRCOUNT. Travelers' `Characteristics`
    (note: plural) carry per-person flags like FQTV (MileagePlus#).
    """
    for c in chars or []:
        if (c.get("Code") or "").upper() == code.upper():
            return c.get("Value") or c.get("Description")
    return None


def _seg_to_leg(fs: dict) -> dict | None:
    """Convert a MyTrips FlightSegment → a flight-shaped leg node.

    MyTrips FlightSegments wrap a nested `FlightSegment` dict with the
    real per-leg data (dep/arr airports, times, flight number, booking
    class). Durations and aircraft aren't included at this tier; the
    caller can enrich via /api/flights/... or accept None.
    """
    seg = fs.get("FlightSegment") or {}
    flight_num = seg.get("FlightNumber")
    if not flight_num:
        return None
    marketing = (seg.get("OperatingAirlineCode") or "UA").strip()
    # Airports: nested records with full city/state/country/lat-lon.
    def _ap(a: dict | None) -> dict | None:
        if not a: return None
        name = a.get("Name") or a.get("ShortName")
        country = (a.get("IATACountryCode") or {}).get("CountryCode")
        state = (a.get("StateProvince") or {}).get("StateProvinceCode")
        return _airport_node_from_ua(
            iata=a.get("IATACode"),
            name=name,
            country_code=country,
            state_code=state,
        )
    bc = (seg.get("BookingClasses") or [{}])[0].get("Code", "").strip()
    # Full flight number format "UA 1234"
    label = f"{marketing} {flight_num}".strip()
    leg: dict = {
        "shape": "flight",
        "id": f"united-flight:{label}",
        "flightNumber": label,
        "departureTime": _parse_mytrips_datetime(seg.get("DepartureDateTime")),
        "arrivalTime":   _parse_mytrips_datetime(seg.get("ArrivalDateTime")),
        "departsFrom":   _ap(seg.get("DepartureAirport")),
        "arrivesAt":     _ap(seg.get("ArrivalAirport")),
        "airline":       _UNITED_ORG if marketing == "UA" else {
            "shape": "airline", "iataCode": marketing, "name": marketing,
        },
        # Cabin comes from BookingClass letter — N = Basic/Economy on UA.
        # Keeping the raw letter for downstream mapping; no faux label.
        "_bookingClass": bc or None,
    }
    return leg


def _itin_to_trips(itin: dict) -> list[dict]:
    """Split a MyTrips itinerary into trip-shaped nodes.

    United returns all flight segments flat in `FlightSegments[]`. Each
    segment has a `TripNumber` that groups legs belonging to the same
    direction (outbound=1, return=2, etc.). We bucket by TripNumber and
    build one trip per bucket, with sorted legs[] + origin/destination
    taken from first/last leg.
    """
    segs = itin.get("FlightSegments") or []
    buckets: dict[int, list[dict]] = {}
    for s in segs:
        tn = int(s.get("TripNumber") or 1)
        buckets.setdefault(tn, []).append(s)
    trips: list[dict] = []
    for tn in sorted(buckets):
        bucket = sorted(buckets[tn], key=lambda s: int(s.get("SegmentNumber") or 0))
        legs = [l for l in (_seg_to_leg(s) for s in bucket) if l]
        if not legs:
            continue
        f0, fL = legs[0], legs[-1]
        origin = f0.get("departsFrom") or {}
        dest   = fL.get("arrivesAt") or {}
        trips.append({
            "shape": "trip",
            "id": f"united-trip:{origin.get('iataCode','?')}-{dest.get('iataCode','?')}:{f0.get('departureTime','')[:10]}",
            "name": f"{origin.get('iataCode','?')}→{dest.get('iataCode','?')}",
            "tripType": "flight",
            "status": "confirmed",
            "departureTime": f0.get("departureTime"),
            "arrivalTime":   fL.get("arrivalTime"),
            "origin":        origin or None,
            "destination":   dest or None,
            "carrier":       _UNITED_ORG,
            "legs":          legs,
            "_tripIndex":    tn,
        })
    return trips


def _itin_to_passengers(itin: dict) -> list[dict]:
    """Map MyTrips Travelers[] → person-shaped nodes.

    Each traveler has a `Person` with GivenName/Surname and a sibling
    `Characteristics[]` with `FQTV` entries (one per frequent-flyer
    program attached to the booking).
    """
    travs = itin.get("Travelers") or []
    out: list[dict] = []
    for t in travs:
        person = t.get("Person") or {}
        given = (person.get("GivenName") or "").strip()
        surname = (person.get("Surname") or "").strip()
        middle = (person.get("MiddleName") or "").strip()
        if not (given or surname):
            continue
        full = " ".join(p for p in [given, middle, surname] if p)
        # MileagePlus (or other FF#) from traveler's own Characteristics
        mp = _characteristic_value(t.get("Characteristics"), "FQTV")
        node: dict = {
            "shape": "person",
            "id": f"united-traveler:{(person.get('Key') or surname or given or 'unknown').strip()}",
            "name": full,
            "givenName": given or None,
            "additionalName": middle or None,
            "familyName": surname or None,
            "legalName": full or None,
        }
        if mp:
            node["memberships"] = [{
                "shape": "membership",
                "id": mp,
                "name": f"MileagePlus {mp}",
                "status": "active",
                "at": _UNITED_ORG,
            }]
        out.append(node)
    return out


@returns("reservation[]")
@connection("web")
@timeout(45)
async def list_trips(upcoming_only: bool = True, **params) -> list[dict]:
    """List upcoming United reservations for the logged-in MileagePlus user.

    Each reservation is emitted as a graph-complete `reservation` node:
    trips[] with origin/destination/times, legs[] with flight numbers +
    airports, passengers[] from the itinerary, and flags derived from the
    MyTrips response (HAS_ETICKET, status, 24-hour void window, etc.).

    Fields we don't yet capture (aircraft type, fare breakdown, totals) are
    left as None rather than fabricated — those enrich cleanly once the
    per-trip detail endpoint is wired up.

    Args:
        upcoming_only: If True (default), fetch only future trips. If False,
          still returns upcoming today-to-next-year (United's MyTripsByMileagePlus
          endpoint doesn't return distant past trips — past-trip history lives
          elsewhere and isn't yet implemented).
    """
    from datetime import date, datetime as _dt, timedelta

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
        # PNR — United ships it as ConfirmationID on MyTrips responses
        # (RecordLocator / ConfirmationNumber were guesses that never matched).
        pnr = (itin.get("ConfirmationID") or itin.get("RecordLocator") or itin.get("ConfirmationNumber") or "").strip()
        if not pnr:
            continue

        trips = _itin_to_trips(itin)
        passengers = _itin_to_passengers(itin)

        # Booking time: MyTrips gives CreateDate in the M/D/YYYY format.
        booked_at = _parse_mytrips_datetime(itin.get("CreateDate"))
        # Trip window — first leg dep → last leg arr.
        start_time = trips[0]["departureTime"] if trips else None
        end_time   = trips[-1]["arrivalTime"] if trips else None
        # 24-hour void window (US DOT rule): if we have a bookingTime, add 24h.
        void_ends = None
        try:
            if booked_at:
                void_ends = (_dt.fromisoformat(booked_at) + timedelta(hours=24)).isoformat()
        except Exception:
            pass

        # Characteristic flags
        has_etkt = (_characteristic_value(itin.get("Characteristic"), "HAS_ETICKET") or "").lower() == "true"
        pnr_status = _characteristic_value(itin.get("Characteristic"), "PNR_STATUS") or ""
        # Map Current/History/... → our reservation.status (confirmed/completed)
        status = "confirmed" if pnr_status.lower() in ("current", "") else pnr_status.lower()

        reservation = {
            "shape": "reservation",
            "id": f"united-pnr:{pnr}",
            "reservationType": "flight",
            "reservationId": pnr,
            "status": status,
            "bookingType": "instant",
            "bookingTime": booked_at,
            "startTime": start_time,
            "endTime":   end_time,
            "voidWindowEndsAt": void_ends,
            "availableActions": ["cancel", "change", "check_in"] if has_etkt else ["cancel", "change"],
            "checkinUrl": f"https://www.united.com/en/us/checkin/{pnr}",
            "partySize": len(passengers) or 1,
            # totals not in MyTrips — deferred to a future get_trip enrichment
            "totalAmount": None,
            "currency": None,
            "name": (itin.get("NickName") or f"United reservation {pnr}").strip(),
            "at": _UNITED_ORG,
            "trips": trips,
            "passengers": passengers,
            "_raw": itin,
            "_hasEticket": has_etkt,
            "_pnrStatus": pnr_status or None,
        }
        reservations.append(reservation)
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
            # United stores phones split into AreaNumber + PhoneNumber (subscriber only).
            area = (primary.get("AreaNumber") or "").strip()
            sub  = (primary.get("PhoneNumber") or "").strip()
            phone_number = (area + sub) if area else sub
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
    # Interior width (display cells). Worst case: ABC DEF layout
    # = 6 seats × 2 cells + 4 SEAT_SEPs × 1 + 1 aisle × 6 cells (premium)
    # = 12 + 4 + 6 = 22. Add margin for breathing room.
    INNER = 28

    # All seats render as emoji (2 terminal cells wide each). Premium
    # cabins (First / Business) get a wider aisle gap — matches UA's UI
    # where First has more space between A|B and E|F — rather than a
    # wider cell, since an emoji is already large.
    #   F = First, J = Business/First (UA domestic), C = Business
    #   W = Premium Economy, Y = Economy
    _PREMIUM_CABIN_TYPES = {"F", "J", "C"}

    # Cell is always one emoji wide. The byte-count of a cell varies
    # (emoji may be 1 or 4+ codepoints), but every cell renders as 2
    # terminal cells. We track cells, not bytes, for layout.
    _CELL_STR_WIDTH = 2  # display cells per seat

    def _aisle_width_cells(cabin: dict | None) -> int:
        """Width (in terminal cells) of the gap between seat groups."""
        if cabin and (cabin.get("cabinType") or "").upper() in _PREMIUM_CABIN_TYPES:
            return 6  # wider — First has ~2 empty cells between AB | EF
        return 4

    # Keycap emojis for paid tiers — one glyph per tier, rendered as 2
    # terminal cells (colorful in every modern emoji font). We rank the
    # flight's paid tiers densely 1..N so unused tier ids don't appear
    # on the map; the legend maps each keycap back to its real price.
    _KEYCAP = [
        "1️⃣","2️⃣","3️⃣","4️⃣","5️⃣",
        "6️⃣","7️⃣","8️⃣","9️⃣","🔟",
    ]

    # Build paid-tier ranking: real-tier-id → (rank_index, price).
    _paid_tiers_sorted = sorted(
        [(tid, (t.get("price") or 0)) for tid, t in tiers_map.items()
         if (t.get("price") or 0) > 0],
        key=lambda x: x[1],
    )
    _tier_rank: dict[int, int] = {tid: i for i, (tid, _) in enumerate(_paid_tiers_sorted)}

    # Glyphs — every one renders as 2 terminal cells wide (emoji or
    # double-cell character). Picked for clarity + color + consistent
    # presentation across modern terminals (iTerm, Terminal.app, Ghostty,
    # Kitty, WezTerm, Alacritty).
    # Seat-state palette — temperature scale so "availability" reads at a
    # glance. Green X (❎) was confusing (green+X = mixed signal), so we
    # use neutral/warm tones instead.
    _GLYPH_BLANK    = "  "       # no seat at this grid column
    _GLYPH_FREE     = "⬜"       # U+2B1C WHITE LARGE SQUARE — open
    _GLYPH_OCCUPIED = "❌"       # U+274C CROSS MARK — taken (someone else has it)
    _GLYPH_BLOCKED  = "🟥"       # U+1F7E5 RED SQUARE — structurally blocked (API-reported)
    _GLYPH_MONUMENT = "▓▓"       # fallback if a monument spills into a seat cell

    def _seat_cell(s: dict | None) -> str:
        """Render one seat — always 2 terminal cells wide."""
        if s is None:
            return _GLYPH_BLANK
        if s.get("itemType") == "MONUMENT":
            return _GLYPH_MONUMENT
        if s.get("isBlocked") or s.get("isPermanentBlocked"):
            return _GLYPH_BLOCKED
        if s.get("isAvailable"):
            tier = int(s.get("tier") or 0)
            price = (tiers_map.get(tier) or {}).get("price") or 0
            if price > 0:
                rank = _tier_rank.get(tier, 0)
                return _KEYCAP[rank] if rank < len(_KEYCAP) else "💰"
            return _GLYPH_FREE
        return _GLYPH_OCCUPIED

    # Between-seat spacing inside a group: one space cell for air.
    _SEAT_SEP = " "

    def _row_line(row: dict, cabin: dict) -> tuple[str, str, str]:
        """Return (left_label, interior, right_label) for a row.

        Cells are 2-display-wide emoji, separated by 1 space inside a
        group, aisle_width_cells spaces between groups. Row number
        prints OUTSIDE the left wall; structural labels (wing / exit /
        bulkhead) print OUTSIDE the right wall.
        """
        layout = cabin.get("layout") or ""
        aisle = " " * _aisle_width_cells(cabin)
        groups = layout.split(" ")
        sl = {
            s.get("letter"): s
            for s in (row.get("seats") or [])
            if s.get("itemType") != "MONUMENT"
        }
        group_strs: list[str] = []
        for g in groups:
            parts = [_seat_cell(sl.get(letter)) for letter in list(g)]
            group_strs.append(_SEAT_SEP.join(parts))
        interior = aisle.join(group_strs)

        rn = str(row.get("number") or "")
        left_label = f"{rn:>3}  "

        labels = []
        if row.get("wing"):
            labels.append("wing")
        if any(s.get("isExit") for s in row.get("seats") or []):
            labels.append("exit-row")
        if any(s.get("isBulkhead") for s in row.get("seats") or []):
            labels.append("bulkhead")
        right_label = " ←  " + ", ".join(labels) if labels else ""
        return left_label, interior, right_label

    import unicodedata as _ud

    def _display_width(text: str) -> int:
        """Width of `text` in terminal cells.

        Rules:
          - Keycap sequences `X U+FE0F U+20E3` render as 2 cells (emoji
            presentation) — detect and count once.
          - Other cells: Wide/Fullwidth EAW → 2, combining/VS/ZWJ → 0,
            everything else → 1.
        """
        w = 0
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            # Keycap sequence detection: base + VS16 + U+20E3.
            if (
                i + 2 < n
                and text[i + 1] == "️"
                and text[i + 2] == "⃣"
            ):
                w += 2
                i += 3
                continue
            # Zero-width combining / variation-selector / ZWJ.
            if _ud.category(ch) in ("Mn", "Me", "Cf"):
                i += 1
                continue
            # Emoji presentation after a base char (emoji VS16) can also
            # promote a text-presentation char to 2 cells (☕ + FE0F → ☕️).
            if i + 1 < n and text[i + 1] == "️":
                w += 2
                i += 2
                continue
            eaw = _ud.east_asian_width(ch)
            if eaw in ("W", "F"):
                w += 2
            else:
                w += 1
            i += 1
        return w

    def _pad(inner: str) -> str:
        """Center `inner` in a line of INNER terminal cells."""
        pad = INNER - _display_width(inner)
        if pad <= 0:
            return inner
        left = pad // 2
        right = pad - left
        return (" " * left) + inner + (" " * right)

    # Glyph map — emoji throughout, each 2 terminal cells wide so they
    # line up with seat cells. ☕ carries U+FE0F (variation selector-16)
    # so terminals that default to text presentation still render it as
    # a colorful emoji. The other choices are single-presentation emoji
    # (no selector needed).
    _MON_GLYPH = {
        "LAV": "🚻",         # U+1F6BB RESTROOM
        "GALLEY": "☕️",  # U+2615 HOT BEVERAGE + VS16 → color
        "CLOSET": "🧺",
        "STOWAGE": "🧺",
        "SPACER": "  ",       # 2-cell blank, keeps alignment
        "AISLE": "  ",
    }

    def _monument_cells_for_row(monuments: list, cabin: dict) -> str | None:
        """Return a row-body string with monuments placed at the
        OUTERMOST column of each seat group, matching UA's UI (the
        lavatory icon spans rows A-B on the left; the galley spans
        rows E-F on the right).

        We don't use the API's `horizontalGridNumber` directly — it
        anchors monuments to the first seat of a group (col 1 for AB,
        col 4 for EF), which gives an inside-aligned look. UA instead
        renders these glyphs at the outer edges of each group.

        Returns None if the layout is missing — caller falls back to
        a centered label.
        """
        layout = cabin.get("layout") or ""
        if not layout:
            return None
        groups = layout.split(" ")
        if not groups:
            return None
        aisle = " " * _aisle_width_cells(cabin)

        # Build grid-column → glyph from the API's horizontal grid.
        # Keep the mapping around only so we know WHICH groups have a
        # monument (left-group vs right-group). We don't use the grid
        # col for layout — we paint at the outer edge of each group.
        by_col: dict[int, str] = {}
        for m in monuments:
            hgn = m.get("horizontalGridNumber")
            if hgn is None:
                continue
            itype = m.get("itemType") or ""
            glyph = _MON_GLYPH.get(itype)
            if not glyph or glyph.strip() == "":
                continue
            by_col[int(hgn)] = glyph

        if not by_col:
            return None

        # Walk grid columns tracking which group we're in. For each
        # group, collect "does it have any monument in any of its
        # columns?" and which glyph (first one wins — rare to have
        # multiple monuments per group per row).
        group_glyphs: list[str | None] = []
        col = 0
        for gi, g in enumerate(groups):
            if gi > 0:
                col += 1  # aisle column
            glyph_for_group: str | None = None
            for _letter in list(g):
                col += 1
                if col in by_col:
                    glyph_for_group = glyph_for_group or by_col[col]
            group_glyphs.append(glyph_for_group)

        # Render each group: the glyph sits at the outer edge (leftmost
        # cell for a left group, rightmost cell for a right group);
        # remaining cells are blank. Middle group (3-group layouts on
        # wide-bodies, e.g. 3-4-3) centers the glyph for now.
        n_groups = len(groups)
        rendered_groups: list[str] = []
        for gi, g in enumerate(groups):
            glyph = group_glyphs[gi]
            width = len(g)
            cells = ["  "] * width
            if glyph:
                if gi == 0:
                    cells[0] = glyph                # leftmost — outer edge
                elif gi == n_groups - 1:
                    cells[-1] = glyph               # rightmost — outer edge
                else:
                    cells[width // 2] = glyph       # middle group — center
            rendered_groups.append(_SEAT_SEP.join(cells))
        return aisle.join(rendered_groups)

    # Build the drawing row-by-row.
    # Row body width (what sits inside the walls) = INNER. A seat row prints
    # as:  "NN  " + "│ " + padded_interior(INNER) + " │" + right_label
    # — row number OUTSIDE the left wall, structural labels OUTSIDE the right.
    TUBE_OUTER = INNER + 4    # "│ " + INNER + " │"
    LEFT_MARGIN = "     "     # 5 chars — matches "NN   " for non-row lines
    lines: list[str] = []
    # Header above the plane — flight + route + **date + time** + aircraft.
    # Parse the ISO departureTime into a friendly "Tue Apr 28, 2026 · 13:00"
    # so there's no ambiguity about which flight we're looking at.
    dep_iso = sm.get("departureTime") or ""
    dep_friendly = dep_iso.replace("T", " ")
    try:
        from datetime import datetime as _dt
        _parsed = _dt.fromisoformat(dep_iso.replace("Z", "+00:00")) if dep_iso else None
        if _parsed is not None:
            dep_friendly = _parsed.strftime("%a %b %-d, %Y · %H:%M")
    except Exception:
        pass
    lines.append(
        f"✈  {sm.get('flightNumber')}  "
        f"{sm.get('origin')} → {sm.get('destination')}  "
        f"{dep_friendly}  "
        f"aircraft {sm.get('aircraftCode')}"
    )
    lines.append("")

    # "A  B  ...  E  F" header — each letter centered under its 2-cell
    # seat slot, with SEAT_SEP between letters within a group and the
    # cabin's aisle-width between groups.
    def _letter_header(cabin: dict) -> str:
        layout = cabin.get("layout") or ""
        if not layout:
            return ""
        aisle = " " * _aisle_width_cells(cabin)
        groups = layout.split(" ")
        group_strs = []
        for g in groups:
            # Each letter is 1 cell; center it in a 2-cell slot.
            parts = [f"{letter} " for letter in list(g)]
            group_strs.append(_SEAT_SEP.join(parts))
        return aisle.join(group_strs)

    # Top cap: a flat box. No nose/tail cone.
    lines.append(LEFT_MARGIN + "┌" + "─" * (TUBE_OUTER - 2) + "┐")

    # UA's visual order per cabin is:
    #   monuments (lavatories / galleys at actual grid positions)
    #   — cabin brand banner ("United First") —
    #   column letters (A B  …  E F)
    #   seat rows
    # We defer the brand+letters until just before the first seat row
    # of each cabin, so the top of each cabin matches the UI.
    written_header_for: set[int] = set()

    def _ensure_cabin_header(cabin_idx: int) -> None:
        if cabin_idx in written_header_for:
            return
        written_header_for.add(cabin_idx)
        cabin = (sm.get("cabins") or [])[cabin_idx] if cabin_idx < len(sm.get("cabins") or []) else {}
        brand = class_sections[cabin_idx]["brand"] if cabin_idx < len(class_sections) else ""
        if brand:
            lines.append(LEFT_MARGIN + "│ " + _pad(brand) + " │")
            # Blank line separating the brand label from the column
            # letters below — matches UA's UI where the class title
            # has vertical breathing room above the seat grid.
            lines.append(LEFT_MARGIN + "│ " + _pad("") + " │")
        lh = _letter_header(cabin)
        if lh:
            # Letters sit IMMEDIATELY above their seat columns — no
            # blank line between letters and the first seat row.
            lines.append(LEFT_MARGIN + "│ " + _pad(lh) + " │")

    # Walk the merged entries and draw row-by-row
    current_cabin = 0
    for kind, vgn, payload, cabin_idx in all_entries:
        # Cabin transition? Draw a cabin-divider band.
        if cabin_idx != current_cabin:
            lines.append(LEFT_MARGIN + "│ " + _pad("— — — — — — — — — — — —") + " │")
            current_cabin = cabin_idx

        if kind == "mon":
            monuments = payload.get("monuments") or []
            has_exit = any(m.get("isDoorExit") for m in monuments)
            if has_exit:
                # Draw a row with red exit markers OUTSIDE the fuselage wall
                lines.append(LEFT_MARGIN + "│▐" + _pad("═ ═ ═  DOOR/EXIT  ═ ═ ═") + "▌│")
                continue

            # Positional monument row — honor horizontalGridNumber so the
            # lavatory shows upper-left, a galley upper-right, etc.
            # Layout is `AB EF` (or `ABC DEF`) — letters + a single space
            # gap that represents the aisle. Monument cells are one column
            # wide each (same as a seat cell) with a 3-char aisle gap.
            cabin = (sm.get("cabins") or [])[cabin_idx]
            cells = _monument_cells_for_row(monuments, cabin)
            if cells is not None:
                lines.append(LEFT_MARGIN + "│ " + _pad(cells) + " │")
                continue
            # Fallback for rows we can't position: centered label.
            types = {m.get("itemType") for m in monuments}
            if "LAV" in types:
                lines.append(LEFT_MARGIN + "│ " + _pad("🚻  LAVATORY  🚻") + " │")
            elif "GALLEY" in types:
                lines.append(LEFT_MARGIN + "│ " + _pad("☕  GALLEY  ☕") + " │")
            continue

        # Seat row — emit cabin header (brand + letters) just before
        # this cabin's first seat row, so the ordering matches UA's UI.
        _ensure_cabin_header(cabin_idx)
        row = payload
        cabin = (sm.get("cabins") or [])[cabin_idx]
        left_label, interior, right_label = _row_line(row, cabin)
        padded = _pad(interior)
        # For exit-row rows, draw small red "▐" / "▌" on the wall edge
        is_exit_row = any(s.get("isExit") for s in row.get("seats") or [])
        left_wall = "│▐" if is_exit_row else "│ "
        right_wall = "▌│" if is_exit_row else " │"
        lines.append(left_label + left_wall + padded + right_wall + right_label)

    # Bottom cap: flat box.
    lines.append(LEFT_MARGIN + "└" + "─" * (TUBE_OUTER - 2) + "┘")

    # Legend — one glyph per line, skimmable.
    legend_lines = [
        "Legend:",
        "  ⬜  free (no charge to select)",
        "  ❌  occupied (someone else has it)",
        "  🟥  permanently blocked (held for elites / day-of-travel / staff)",
        "  🚻  lavatory",
        "  ☕️ galley",
    ]

    # Price tiers: show each keycap emoji alongside its price, ranked
    # cheapest→most expensive. Ties what 1️⃣ / 2️⃣ / … on the map cost.
    if _paid_tiers_sorted:
        legend_lines.append("")
        legend_lines.append("Paid seats:")
        for rank, (_tid, price) in enumerate(_paid_tiers_sorted):
            glyph = _KEYCAP[rank] if rank < len(_KEYCAP) else "💰"
            legend_lines.append(f"  {glyph}   ${price:.2f}")

    if sm.get("basicEconomyLocked"):
        legend_lines.append("")
        legend_lines.append(
            "⚠ Basic Economy fare: seat selection is not purchasable on this ticket."
        )

    legend = "\n".join(legend_lines)
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


async def _fetch_saved_addresses_for_user() -> list[dict]:
    """Fetch the logged-in user's saved addresses from /api/user/addresses.

    Each address carries a `Key` string that cards reference via `AddressKey`
    — that's how the checkout page resolves "which billing address goes with
    the selected card". `IsPrimary` is unrelated to booking billing; it
    tracks a user-level preference that may not match the selected card.
    """
    try:
        resp = await _authed_get("/api/user/addresses")
        if resp.get("status") != 200:
            return []
        return ((resp.get("json") or {}).get("data") or {}).get("Addresses") or []
    except Exception:
        return []


def _billing_address_for_card(card: dict, addresses: list[dict]) -> dict | None:
    """Resolve the billing address a card points at via its AddressKey.

    Returns a shape-friendly dict: line1, line2, city, state, postalCode,
    country. Returns None when the card has no AddressKey or no matching
    address is on file.
    """
    akey = (card or {}).get("AddressKey")
    if not akey:
        return None
    for a in addresses:
        if a.get("Key") == akey:
            return {
                "shape": "postal_address",
                "line1":      a.get("AddressLine1"),
                "line2":      a.get("AddressLine2"),
                "city":       a.get("City"),
                "state":      a.get("StateCode") or a.get("State"),
                "postalCode": a.get("PostalCode"),
                "country":    a.get("CountryCode") or a.get("CountryName"),
            }
    return None


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


def _render_booking_review(
    *,
    cart: dict,
    dc_raw: dict,
    traveler: dict | None,
    payment_method: dict | None,
    billing_address: dict | None,
    tax_lines: list[dict],
    base_amount: float,
    total_amount: float,
    currency: str,
    save_card_toggled: bool | None,
    insurance_declined: bool | None,
    free_cancel_24h: bool = True,
    width: int = 72,
) -> str:
    """Render the full booking-review card as a framed ASCII block.

    Matches the UI of the checkout page's right-rail summary. Callers
    typically render this into an AskUserQuestion preview or print it
    before calling `confirm_booking`. All inputs are pre-resolved —
    this function does not hit the network.
    """
    import datetime as _dt
    W = width
    def L(s: str) -> str: return "│ " + s.ljust(W-4) + " │"
    def S(title: str) -> str:
        t = f" {title} "; return "├" + t + ("─"*(W-3-len(t))) + "┤"
    def spaced(left: str, right: str) -> str:
        pad = max(1, W - 4 - len(left) - len(right))
        return L(left + " "*pad + right)

    # Top frame
    out: list[str] = []
    cart_ref = cart.get("_cartRefId") or (cart.get("_cartId") or "")[:8]
    out.append("┌" + "─"*(W-2) + "┐")
    hdr = "  UNITED AIRLINES · BOOKING REVIEW"
    if cart_ref: hdr += f" · Cart #{cart_ref}"
    out.append(L(hdr))
    trips = cart.get("trips") or []
    pax_count = int(((dc_raw.get("DisplayPrices") or [{}])[0]).get("Count") or 1)
    is_rt = len(trips) == 2
    cabin_label = "Basic Economy"  # TODO: infer from ProductCode / fare family
    subline = f"  {pax_count} traveler{'s' if pax_count != 1 else ''} · {cabin_label} · "
    subline += "round-trip" if is_rt else ("one-way" if len(trips) == 1 else f"{len(trips)} segments")
    if dc_raw.get("IsNonRefundable"): subline += " · Non-refundable"
    out.append(L(subline))

    # Traveler section — optional
    if traveler:
        out.append(S("TRAVELER"))
        name_line = " ".join(p for p in [
            traveler.get("GivenName") or traveler.get("given_name"),
            traveler.get("MiddleName") or traveler.get("middle_name"),
            traveler.get("Surname") or traveler.get("surname"),
        ] if p)
        if name_line:
            out.append(L(f"  {name_line}"))
        meta = []
        dob = traveler.get("BirthDate") or traveler.get("date_of_birth")
        if dob:
            try:
                d = _dt.datetime.fromisoformat(str(dob).replace("Z","+00:00"))
                meta.append("DOB " + d.strftime("%m/%d/%Y"))
            except Exception:
                meta.append("DOB " + str(dob)[:10])
        gender = traveler.get("GenderCode") or traveler.get("gender")
        if gender: meta.append(f"Gender {gender}")
        mp = traveler.get("MileagePlusId") or traveler.get("mileage_plus")
        if mp: meta.append(f"MP# {mp}")
        if meta: out.append(L("  " + " · ".join(meta)))
        ktn = traveler.get("known_traveler_number") or traveler.get("KTN")
        if not ktn:
            for d in traveler.get("Documents") or []:
                if d.get("KnownTravelerNumber"): ktn = d["KnownTravelerNumber"]; break
        if ktn: out.append(L(f"  KTN (TSA PreCheck): {ktn}"))
        phone = traveler.get("primary_phone") or traveler.get("phone")
        cc = traveler.get("primary_phone_country_code") or "1"
        if phone:
            p = str(phone)
            if len(p) >= 10:
                out.append(L(f"  Phone: +{cc} ({p[:3]}) {p[3:6]}-{p[6:]}"))
            else:
                out.append(L(f"  Phone: +{cc} {p}"))
        email = traveler.get("primary_email") or traveler.get("email")
        if email: out.append(L(f"  Email: {email}"))

    # Itinerary — use the already-enriched cart trips
    if trips:
        out.append(S("ITINERARY"))
        for i, trip in enumerate(trips):
            legs = trip.get("legs") or []
            if not legs: continue
            f0, fL = legs[0], legs[-1]
            origin = (f0.get("departsFrom") or {})
            dest   = (fL.get("arrivesAt") or {})
            dep_ts = f0.get("departureTime")
            arr_ts = fL.get("arrivalTime")
            def _p(ts):
                if not ts: return None
                try: return _dt.datetime.fromisoformat(str(ts))
                except Exception:
                    try: return _dt.datetime.strptime(str(ts)[:16], "%Y-%m-%dT%H:%M")
                    except Exception: return None
            d1, d2 = _p(dep_ts), _p(arr_ts)
            when = (d1.strftime("%a %b %-d · %-I:%M %p") if d1 else "") + \
                   (" → " + d2.strftime("%-I:%M %p") if d2 else "")
            # Flight number may already be prefixed (e.g. "UA 1336"); don't
            # double it. Normalize to "<carrier> <num>" with a single space.
            def _flight_label(l: dict) -> str:
                fn = str(l.get("flightNumber","?") or "?").strip()
                carrier = ((l.get("airline") or {}).get("iataCode") or "UA").strip()
                if fn.upper().startswith(carrier.upper()):
                    return fn  # already "UA 1336"
                return f"{carrier} {fn}"
            flight_nums = ", ".join(_flight_label(l) for l in legs)
            aircraft = (legs[0].get("aircraft") or {}).get("name") or ""
            stops = "Nonstop" if len(legs) == 1 else f"{len(legs)-1} stop"
            o_str = f"{origin.get('city') or origin.get('iataCode')} ({origin.get('iataCode','?')})"
            d_str = f"{dest.get('city') or dest.get('iataCode')} ({dest.get('iataCode','?')})"
            out.append(L(f"  {o_str} → {d_str}"))
            out.append(L(f"    {when}  ·  {stops}"))
            out.append(L(f"    {flight_nums}{'   ·   '+aircraft if aircraft else ''}"))
            if i < len(trips) - 1: out.append(L(""))

    # Price
    out.append(S("PRICE"))
    dp0 = (dc_raw.get("DisplayPrices") or [{}])[0]
    pax_desc = (dp0.get("Description") or "adult").lower()
    taxes = sum((tl.get("amount") or 0) for tl in tax_lines)
    out.append(spaced(f"  Fare ({pax_count} {pax_desc})", f"${(base_amount or 0):.2f}"))
    out.append(spaced(f"  Taxes and fees", f"${taxes:.2f}"))
    out.append(L("  " + "─"*(W-8)))
    out.append(spaced(f"  TOTAL DUE", f"${total_amount:.2f} {currency}"))

    # Payment
    if payment_method or billing_address:
        out.append(S("PAYMENT"))
        if payment_method:
            name = payment_method.get("displayName") or "(card)"
            exp_m, exp_y = payment_method.get("expMonth"), payment_method.get("expYear")
            exp_str = f"  (exp {exp_m}/{exp_y})" if exp_m and exp_y else ""
            out.append(L(f"  Card:     {name}{exp_str}"))
        if billing_address:
            line = (billing_address.get("line1") or "").strip()
            if billing_address.get("line2"): line += f", {billing_address['line2']}"
            out.append(L(f"  Billing:  {line}"))
            city = billing_address.get("city") or ""
            state = billing_address.get("state") or ""
            postal = billing_address.get("postalCode") or ""
            addr2 = f"{city}, {state} {postal}".strip(" ,")
            if addr2: out.append(L(f"            {addr2}"))

    # Consents
    out.append(S("CONSENTS"))
    out.append(L(f"  {'[x]' if save_card_toggled     else '[ ]'}  Save card for airport & inflight purchases"))
    out.append(L(f"  {'[x]' if insurance_declined    else '[ ]'}  Declined Travel Guard insurance"))
    out.append(L(f"  {'[x]' if free_cancel_24h       else '[ ]'}  Cancel for free within 24 hours of booking"))
    out.append("└" + "─"*(W-2) + "┘")

    # Tax detail (below the card so users can see without filling the frame)
    if tax_lines:
        out.append("")
        out.append(f"  Tax breakdown ({len(tax_lines)} line items):")
        for tl in tax_lines:
            desc = tl.get("description", "?")
            amt = f"${(tl.get('amount') or 0):.2f}"
            dots = max(2, W - 6 - len(desc) - len(amt))
            out.append(f"    {desc}{' '+'·'*(dots-2)+' '}{amt}")

    return "\n".join(out)


@returns("booking_offer")
@connection("web")
@timeout(30)
async def prepare_booking(
    *,
    cart_id: str,
    signature_ttl_seconds: int = 300,
    save_card_for_inflight: bool | None = None,
    insurance_declined: bool | None = None,
    **params,
) -> dict:
    """Produce a signed booking_offer node from the current cart state.

    This is the safe, read-only half of the booking gate. It fetches
    the live cart via LoadReservationAndCart, the user's saved cards
    via /api/user/creditCards, their saved addresses via
    /api/user/addresses (to resolve the selected card's billing via
    AddressKey), and composes a rich `booking_offer` node:

      - trips[] — enriched with airport city/state/country and aircraft
        manufacturer/model
      - fares[] — one per fare component, with fareBasisCode, class
      - taxLines[] — every tax/fee line itemized (US Transportation Tax,
        XF Passenger Facility Charge, AY Security Fee, etc.)
      - paymentMethod — the currently selected saved card (IsSelected=true
        from /api/user/creditCards), with last4, brand, expiry, and
        opaque providerTokens
      - billingAddress — the address a card points at (resolved via
        card.AddressKey → /api/user/addresses). This is NOT always the
        user's IsPrimary address; cards carry their own billing.
      - totalAmount, baseAmount, taxAmount, currency
      - referenceNumber — the short human-readable cart ID United shows
        on screen (641457887)
      - cartId — the long UUID
      - HMAC-signed blob (v3) and expiresAt
      - review — framed ASCII card, ready to print or feed into an
        AskUserQuestion preview

    The signed `blob` (also available as `_signature` on the returned
    node) is required by confirm_booking to actually charge the card.
    The agent cannot forge a blob; only a live cart read can mint one.

    Args:
        cart_id: UUID of the cart to book.
        signature_ttl_seconds: how long the signature is valid (default
            300s / 5 min). Short TTL prevents stale-blob replay.
        save_card_for_inflight: if True, user has consented to saving the
            selected card for airport/inflight tap-to-pay on this trip.
            Reflected in the consent row of the rendered review AND
            stored in the signed blob so confirm_booking can verify.
        insurance_declined: if True, user has explicitly declined Travel
            Guard insurance on the checkout page. Blob-verified.
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

    # Billing address — each card points at an address via AddressKey.
    # Not the user's `IsPrimary` address; this is the card's own billing.
    saved_addresses = await _fetch_saved_addresses_for_user()
    billing_address = _billing_address_for_card(selected_card, saved_addresses) if selected_card else None

    # Travelers — prefer registered-traveler detail (name/DOB/gender/MP#/KTN
    # from the cart's committed state) over DisplayTravelers (which only
    # carries pax-type + DOB). `register_traveler` writes to Travelers[]
    # inside the reservation envelope.
    travelers = dc_raw.get("DisplayTravelers") or []
    committed_traveler = None
    try:
        # The cart's _raw has a Reservation nested inside with committed traveler detail
        res = dc_raw.get("Reservation") or {}
        res_travs = res.get("Travelers") or []
        if res_travs:
            p0 = (res_travs[0] or {}).get("Person") or {}
            committed_traveler = {
                "GivenName":     p0.get("GivenName"),
                "MiddleName":    p0.get("MiddleName"),
                "Surname":       p0.get("Surname"),
                "BirthDate":     p0.get("BirthDate"),
                "GenderCode":    p0.get("GenderCode") or p0.get("Sex"),
                "MileagePlusId": p0.get("MileagePlusId") or ((p0.get("FrequentFlyerPrograms") or [{}])[0] or {}).get("Number"),
                "Documents":     p0.get("Documents") or [],
            }
    except Exception:
        committed_traveler = None
    # Fall back to the user's profile if nothing's committed yet.
    if not committed_traveler:
        try:
            contact = await get_contact_info()
            committed_traveler = contact
        except Exception:
            committed_traveler = None

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

    # Signed payload — everything bound by the signature. Consent flags
    # and booking-time decisions are carried too so `confirm_booking` can
    # reject replays with mismatched consents.
    signed_payload = {
        "version": 3,
        "cart_id": cart_id,
        "reference_number": cart.get("_cartRefId"),
        "total_amount": total,
        "currency": currency,
        "itinerary_hash": itinerary_hash,
        "payment_method_last4": (payment_method or {}).get("last4"),
        "payment_method_identifier": (payment_method or {}).get("identifier"),
        "payment_method_address_key": (payment_method or {}).get("providerTokens", {}).get("addressKey"),
        "search_type": dc_raw.get("SearchType"),
        "prepared_at": now,
        "expires_at": expires_at,
        # Session-4 additions: we record agent-stated consents so confirm
        # can verify them. These default to None — caller-set to True/False
        # once the ASCII review has been shown to the user.
        "save_card_for_inflight":   save_card_for_inflight,
        "insurance_declined":        insurance_declined,
    }
    signature = _sign_blob(signed_payload)
    blob = {**signed_payload, "_signature": signature}
    blob_str = base64.b64encode(_j.dumps(blob).encode("utf-8")).decode("ascii")

    amount_str = f"{currency} {total:.2f}"

    # Render the structured ASCII review. We pass in everything pre-resolved
    # so the renderer does no I/O.
    review_text = _render_booking_review(
        cart=cart,
        dc_raw=dc_raw,
        traveler=committed_traveler,
        payment_method=payment_method,
        billing_address=billing_address,
        tax_lines=tax_lines,
        base_amount=base_amount or 0,
        total_amount=total,
        currency=currency,
        save_card_toggled=save_card_for_inflight,
        insurance_declined=insurance_declined,
    )
    review_text += (
        f"\n\n  Expires in {int(signature_ttl_seconds/60)} min. To book, call:\n"
        f'  confirm_booking(blob=<above>, confirm_amount="{amount_str}", '
        f'payment_method_last4="{(payment_method or {}).get("last4","????")}", dry_run=False)\n'
    )

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
    5. Blob v3+ consent flags (`save_card_for_inflight`, `insurance_declined`)
       must be explicit True/False values. None is treated as "prepare_booking
       was called without the review being shown to the user" and rejected —
       we don't default consents silently when money is on the line.
    6. `dry_run` must be explicitly set to False. Default is True.
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

    # 5. Consents — session-4 blob (v3+) carries save_card_for_inflight and
    #    insurance_declined. Confirm that the agent has recorded them
    #    explicitly (True/False). Leaving them as None means prepare_booking
    #    was called without showing the user a review card — reject.
    if decoded.get("version", 1) >= 3:
        for consent_key in ("save_card_for_inflight", "insurance_declined"):
            val = decoded.get(consent_key)
            if val is None:
                raise RuntimeError(
                    f"confirm_booking: blob is missing consent '{consent_key}'. "
                    f"prepare_booking must be re-run with that flag explicitly "
                    f"set to True or False (after showing the review card to the user). "
                    f"We do not default consents silently."
                )

    # 6. dry_run gate
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

