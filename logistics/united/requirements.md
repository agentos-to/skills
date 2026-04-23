# United Airlines — reverse engineering notes

Captured endpoints, auth details, and data shapes from united.com.

Reverse-engineered: 2026-04-23.

## Frontend stack

- **Custom React SPA** (NOT Next.js — no `__NEXT_DATA__`). Webpack chunks at
  `/public/<hash>/e/<N>.js` and root chunks at `/runtime.<hash>.js`,
  `/main.<hash>.js`, `/<N>.<hash>.js`.
- **State:** Redux with Immutable.js, persisted via `redux-persist` into
  IndexedDB at `localforage.keyvaluepairs.reduxPersist:global` (transit-js
  encoded).
- **Network:** axios; interceptor adds `X-Authorization-api: bearer <hash>`
  from Redux store's `apiToken.hash`.
- **Protection:** Akamai Bot Manager (obfuscated sensor data POSTs at
  randomized paths like `/favQ5fU6ptzCHIs0gPYD/6Lp3StQ9YwLE/...`). Cookie
  `bm_sz`, `_abck`, `bm_sv`, `bm_so`, `ak_bmsc`, `akavpau_ualwww`,
  `akacd_*`. HTTP/2 seems to work fine so far.
- **Tracking:** Optimizely, Qualtrics, Tealium, Dynatrace, Quantum Metric,
  Securiti.ai consent. Third-party scripts — none of them relevant to our
  replay surface.

## Auth

### Cookie tier
Cookie domain `.united.com`. Critical cookies:

| Cookie | Purpose | Example prefix |
|--------|---------|----------------|
| `AuthCookie` | Opaque auth session token | hex64 chars |
| `Session` | Contains `AuthToken=DAAAAJ...` (URL-encoded). User-scope session. | `DAAAAJ` |
| `User` | Contains `RememberID=DAAAAP...`. "Remember me" credential. | `DAAAAP` |
| `PIM-SESSION-ID` | PIM-layer session ID | 16-char string |
| `1pc_session` | First-party session UUID | UUID |
| `_ucid` | User client ID hash | hex |
| `SID` | Presence marker (`true`) | bool string |

Akamai Bot Manager cookies (`bm_*`, `_abck`, `ak_bmsc`, `akavpau_*`,
`akacd_*`) — passed through; don't strip or Akamai will flag as bot.

### Bearer token tier

Short-lived bearer (~30 min TTL, `expiresAt` returned from mint endpoint).
Token envelope is a custom binary format, base64-encoded:

```
0c 00 00 00 <12 bytes ??> 10 00 00 00 <16 bytes ??> <payload...>
```

Different cookie values (`AuthCookie`, `Session.AuthToken`,
`User.RememberID`) all share this same envelope shape but carry different
payloads / lengths.

**Minting:** `GET /api/auth/anonymous-token` (with cookies). Despite the
name, it returns a USER-SCOPED token when valid session cookies are
present.

```
GET https://www.united.com/api/auth/anonymous-token
Cookie: Session=AuthToken=...; User=RememberID=...; AuthCookie=...; (+ Akamai cookies)
Accept: application/json

→ 200
{
  "data": {
    "token": {
      "hash": "DAAAAIFt0c/Mzq/xp8P...",     # the bearer
      "expiresAt": "2026-04-23T18:17:35.0000000+00:00"
    }
  }
}
```

Use the hash as `X-Authorization-api: bearer <hash>` on all subsequent API
calls. Refresh via the same endpoint when within ~5 min of `expiresAt`.

**Validation:** `GET /api/auth/validate-token` — accepts no bearer; returns
`{"valid": true|false, "TokenExpiration": "<seconds>"}` if a bearer is
sent. Also returns `{"valid": false}` (200) if called WITHOUT a bearer.
Useful for session-health check.

### Auth endpoints inventory

| Method | Path | Purpose |
|--------|------|---------|
| GET    | `/api/auth/anonymous-token` | Mint bearer (user-scoped if cookies) |
| GET    | `/api/auth/validate-token`  | Validate bearer |
| ?      | `/api/auth/refresh-token`   | (not probed yet; POST=405, GET probably=405) |
| ?      | `/api/auth/sso-token`       | SSO (not probed yet) |
| POST?  | `/xapi/auth/signin`         | Primary sign-in flow (reserve — user-driven only) |
| POST?  | `/api/auth/signInAfterEnroll` | Post-enroll signin |
| GET    | `/api/auth/signout`         | Sign out |
| POST?  | `/api/auth/randomsecurityquestions` | 2FA challenge |
| POST?  | `/api/auth/SubmitSecurityQuestionsResponses` | 2FA submit |

Plan: **never implement signin** — rely on cookie auth from Brave
(`brave-browser` provider), same pattern as Uber/Amazon skills. If session
expires, the skill returns `SESSION_EXPIRED:` and the engine retries with
fresher cookies.

## Read endpoints

Captured 2026-04-23 from an authenticated `manageres/mytrips` page load.
All require `X-Authorization-api: bearer <hash>`.

### User / account

| Method | Path | Notes |
|--------|------|-------|
| GET | `/xapi/myunited/User/profile` | Full profile: name, MP#, CustomerId, ProfileId, travelers array, title, addresses, phones. THE KEY ENDPOINT for a logged-in session. |
| GET | `/api/myunited/user/balances` | MileagePlus balances: miles (`RDM`), Plus Points Exchange, travel bank, chase certificates, PQPs. |
| GET | `/api/user/creditCards` | Saved cards (last 4, CC type, billing address, token handles). |
| GET | `/api/User/FutureFlightCreditsResiduals` | ETCCertificates, FFCRCertificates, FFCCertificates. |
| POST | `/api/User/ElectronicTravelCertificates?toCurrencyCode=USD` | Active travel certs. |

### Trips

| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/api/mytrips/MyTripsByMileagePlus/` | `{"NumberOfItineraries": int, "StartDate": "MM/DD/YYYY", "EndDate": "MM/DD/YYYY"}` | Upcoming trips. Empty `Data: []` if none. Accepts `NumberOfItineraries: 0` for count-only. |
| POST | `/api/user/trips` | `false` | Kicks off async trip backfill (202 Accepted). Likely polled via separate endpoint (TBD). |

### Reference data

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/referenceData/countries` | Country list with codes. |
| GET | `/api/referenceData/nearestAirport/{lat}/{lng}/{radius}/{resultLimit}` | Nearest airports. |
| GET | `/api/sdl/GetSDLRawContent?page=/ual/en/us/fly/_system/SearchPopularTerms.html` | CMS content. |
| GET | `/api/home/advisories` | Travel advisories. |

## Profile sub-endpoints (captured 2026-04-23 from `/en/us/account/profile`)

All return `{data: ...}` wrapping the actual payload. All require
bearer + cookies.

### Contact info

| Method | Path | Payload shape |
|--------|------|---------------|
| GET | `/api/user/phoneNumbers` | `.data.PhoneNumbers[]` — each has `ChannelCode`, `ChannelTypeCode` (H=Home, O=Other), `CountryCode`, `CountryPhoneNumber`, `AreaNumber`, `PhoneNumber`, `Description` ("Cell"/"Office"), `IsPrimary`, `IsDayOfTravel`, `PhoneDevices[].CommDeviceTypeCode` (WP=wireless phone), `IsVerified`, `VerificationDate`, `Key` (opaque handle for updates) |
| GET | `/api/user/emailAddresses` | `.data.EmailAddresses[]` — `EmailAddress`, `IsPrimary`, `IsDayOfTravel`, `IsVerified`, `Description` ("Home"/"Work"), `Key`. Also `.data.VerifyTrackId` for round-trip updates. |
| GET | `/api/user/addresses` | `.data.Addresses[]` — `AddressLine1`/`AddressLine2`, `City`, `StateCode`, `PostalCode`, `CountryCode`, `ChannelTypeCode` (H=Home), `IsPrimary`, `Key`. |

### Nationality / residence

| Method | Path | Payload |
|--------|------|---------|
| GET | `/api/myunited/user/residenceAndNationality` | `.data.CountryOfResidence` (ISO alpha-2), `.data.Nationality` (ISO alpha-2). **User may leave stale values here — don't treat as ground truth.** |

### Secure Flight / Known Traveler

| Method | Path | Payload shape |
|--------|------|---------------|
| GET | `/api/user/travelerSupplementaryTravelInfo` | `.data.SupplementaryTravelInfos[]` — each has `Number` (the ID digits), `SeqNumber`, `Type` (1-char code). Plus `.data.SecureTraveler` with `DocumentType` (1-char), `SequenceNumber`. |

**Observed `Type` codes** (partial — inferred from single-sample data; confirm as we add more memberships):
- `K` — Known Traveler Number (TSA PreCheck / Global Entry / Nexus — all share the KTN field)
- (not yet seen, plausible): `R` — Redress number. `P` — Passport. Probe when needed.

**Observed `SecureTraveler.DocumentType` codes** (partial):
- `C` — probably *citizen ID* or similar. Unconfirmed.

### Partner loyalty programs

| Method | Path | Payload shape |
|--------|------|---------------|
| GET | `/api/myunited/user/airlinePartnerLoyaltyAccounts` | `.data.FlightRewardProgramList[]` — `ProgramName`, `ProgramVendorName`, `ProgramID` (numeric), `ProgramMemberID` (FF number), `ProgramEnrollDate`, `AirPreferenceId`, `Key`. The user's United MileagePlus itself appears here as a row (ProgramID=7). |
| GET | `/api/referenceData/loyaltyPrograms/` | Reference list of all linkable airline loyalty programs (Star Alliance + partners). |

### Family / member linkage

| Method | Path | Notes |
|--------|------|-------|
| GET | `/xapi/myunited/memberlinkage` | Linked profiles. Returns `{status:"Failure", errors:[{code:"404","message":"Consent not found"}]}` when the user hasn't opted into family linkage — NOT an error, just "absent". Skill should treat as empty set. |

### Profile preferences (captured, not yet explored)

Lower-priority — capture bodies later if a tool needs them:

- `GET /api/myunited/user/PmdPreferences` — personal mobility device preferences
- `GET /api/myunited/user/Preferences` — general preferences
- `GET /api/myunited/user/marketingCommunicationPreferences` — email/SMS opt-in state
- `GET /api/myunited/user/petInCabin` — pet travel preferences
- `GET /api/myunited/user/serviceAnimals` — service animal preferences
- `GET /api/referenceData/MilitaryOrganizations` — reference list (for military fare eligibility)
- `GET /xapi/myunited/memberaffiliate/military/status` — military affiliation

## Graph modeling notes (from captures so far)

**Emails are on accounts, not persons.** Joe uses a per-provider email
pattern (e.g. `united@contini.co` for United, `anthropic@contini.co`
elsewhere) — an intentional spam-source detection scheme. Email belongs
on the `account` node (which is tied to the issuer / platform), not
duplicated onto `person`. United's `emailAddresses` payload → create
one `account` per email (issuer = "united.com" for the primary, or the
email's own domain if we want finer-grained tracking).

**Phone numbers — not modeled as their own shape yet.** They're owned
by the person but tied to a verification state and marked for
day-of-travel contact. Open question: add a `phone` shape, or put them
as string fields on `person`, or as an array on `account`? Current read
is that phones are Person-owned (they survive a platform rebrand /
account closure) but individual-verification-state is platform-scoped.
Probably a future `phone` shape with `holder: person` + `verifiedBy:
account[]` edges. Not urgent; leave out of v1.

**Addresses are places.** Each address entry → upsert `place` with
`fullAddress` / `city` / `region` / `postalCode` / `countryCode` and
link `person --has_address--> place` with the `ChannelTypeCode` (Home/
Business) as an edge value. Multiple addresses = multiple edges.

**KTN = `membership` at TSA.** Per our agreed model:
- `membership.at` → the TSA organization (separate `organization` node)
- `membership.id` → the KTN digits
- `membership.tier` → "PreCheck" / "Global Entry" / "Nexus" / "SENTRI"
  (inferred from which program issued the KTN — United doesn't say which)

Since United's API only gives us `Number` + `Type: "K"` without telling
us WHICH program issued it, we can't distinguish PreCheck vs Global
Entry from United alone. A future skill (e.g. `tsa` skill or a Global
Entry lookup) could enrich. For now, surface it as `Known Traveler
Number` with tier = null.

**Partner loyalty programs = `membership[]` per airline.** One
`membership` per airline in `FlightRewardProgramList` (excluding United
itself, which is already surfaced as MileagePlus). `at` = the partner
airline, `id` = `ProgramMemberID`, `published` = `ProgramEnrollDate`.

## Flight search

### Endpoint

```
POST https://www.united.com/api/flight/FetchSSENestedFlights
X-Authorization-api: bearer <hash>
Content-Type: application/json
Accept: text/event-stream  (response is SSE)
```

**Response is a Server-Sent Events stream** (Content-Type isn't `application/json`;
each line is `data: <json>\n\n`). A CDP `getResponseBody` on a finished SSE
request often returns empty — you must tee the stream while it's in flight
(in the skill: read line-by-line from the HTTP response).

### Deep-link URL (drives the React SPA to fire the search automatically)

```
https://www.united.com/en/us/fsr/choose-flights
  ?f=AUS              origin IATA
  &t=SFO              destination IATA
  &d=2026-04-28       outbound date (YYYY-MM-DD)
  &r=2026-05-03       return date (YYYY-MM-DD, round-trip)
  &px=1               passenger count
  &tt=1               trip type (1 = round-trip, 0 = one-way?)
  &taxng=1            taxes included in displayed prices
  &clm=7              cabin code (7 = Economy including Basic)
  &st=bestmatches     sort order
  &idx=1              slice index (1 = outbound first, 2 = return)
  &mm=0               money+miles toggle
```

### Request body (captured)

```json
{
  "SearchTypeSelection": 1,
  "SortType": "bestmatches",
  "SortTypeDescending": false,
  "Trips": [
    {
      "Origin": "AUS",
      "Destination": "SFO",
      "DepartDate": "2026-04-28",
      "Index": 1,
      "TripIndex": 1,
      "SearchRadiusMilesOrigin": 0,
      "SearchRadiusMilesDestination": 0,
      "DepartTimeApprox": 0,
      "SearchFiltersIn": {
        "FareFamily": "ECONOMY",
        "AirportsStop": null,
        "AirportsStopToAvoid": null,
        "ShopIndicators": {
          "IsTravelCreditsApplied": false,
          "IsDoveFlow": true
        }
      }
    }
  ],
  "CabinPreferenceMain": "economy",
  "PaxInfoList": [
    {"PaxType": 1}
  ],
  "AwardTravel": false,
  "NGRP": false,
  "CalendarLengthOfStay": 0,
  "PetCount": 0,
  "RecentSearchKey": "AUSSFO4/28/2026",
  "CalendarFilters": {"Filters": {"PriceScheduleOptions": {"Stops": 1}}},
  "Characteristics": [
    {"Code": "SOFT_LOGGED_IN", "Value": false},
    {"Code": "UsePassedCartId", "Value": false}
  ],
  "FareType": "Refundable",
  "BuildHashValue": "true",
  "EnableBasicPremiumProducts": true
}
```

**Key observations**:
- `Trips[]` contains ONE slice at a time. Round-trip search fires twice: once for outbound (Index=1), once for return (Index=2) after the outbound is chosen.
- `PaxInfoList[].PaxType`: 1 = ADT (adult). Other codes presumed: 2/3 for CHILD, INF.
- `CabinPreferenceMain`: "economy" | "business" | "first" (verified: economy).
- `CabinPreference` / `clm` URL param mapping needs a second capture to confirm ("clm=7" = Economy including Basic, vs some other number for business).
- `EnableBasicPremiumProducts: true` surfaces Basic Economy as a fare option. Set to `false` to hide.
- `AwardTravel: true` switches to miles pricing.

### Response SSE event types (seen in AUS→SFO Apr 28 Basic Economy search)

Events arrive in order. Each `data: <json>\n\n`.

| Event `type` | Qty | Purpose |
|--------------|-----|---------|
| `meta` | 1 | Search context — **`cartId`** (UUID), origin, destination, date, `tripNumber`, `lastResultId`, `version`, `isLastFlightToBeSelected` (true = all slices picked, ready to checkout). |
| `columns` | 1 | Fare column headers for the results matrix: `refundable[]` and `nonRefundable[]` arrays of `{columnHeader, fareFamily, columnId}`. Drives the UI matrix display. |
| `farefamilies` | 1 | List of `{productType, name, description}` — canonical fare-family descriptions (ECO-BASIC, ECONOMY, ECONOMY-UNRESTRICTED, ECONOMY-MERCH-EPLUS, ECONOMY-UNRESTRICTED-MERCH-EPLUS, MIN-BUSINESS-OR-FIRST, MIN-BUSINESS-OR-FIRST-UNRESTRICTED). |
| `specialPricingInfo` | 1 | Contextual pricing flags. |
| `airports` | 1-many | Dictionary of airport codes referenced in results (`{code, name, countryCode}`). |
| `equipments` | 1-many | Aircraft type dictionary (`{equipmentType: "738"`, `equipmentDescription: "Boeing 737-800"`, door dimensions}`). |
| `cabinCodes` | 1 | Dictionary of short cabin codes (UE=United Economy, UF=United First). |
| `flightOption` | N | **THE FLIGHT RESULTS.** One per returned itinerary. See shape below. |
| `progress` | 1-2 | Streaming progress markers. |
| `flags` | 1 | Feature flags for the result set. |
| `filters` | 1 | Available refinement filters (stops, airlines, times). |
| `teasers` | 1 | Upsell cards. |
| `streamingTimings` | 1 | Backend timing diagnostics. Last event in the stream. |

### `flightOption` shape (verbatim captured keys)

```js
{
  type: "flightOption",
  seq: 1,                    // ordering in stream
  flight: {
    flightNumber: "1336",           // no carrier prefix; see marketingCarrier
    marketingCarrier: "UA",         // who sells it
    marketingCarrierDescription: "United Airlines",
    operatingCarrier: "UA",         // who flies it (may differ — Express)
    operatingCarrierDescription: "United Airlines",
    originalFlightNumber: "2177",   // pre-codeshare-mapping
    parentFlightNumber: "",         // for connection children
    origin: "AUS",
    destination: "SFO",
    departDateTime: "2026-04-28 13:00",    // local to origin, naive (no TZ)
    destinationDateTime: "2026-04-28 15:02", // local to destination, naive
    destinationTerminal: "3",
    orgTimezoneOffset: -5,          // hours from UTC
    destTimezoneOffset: -7,
    destinationTimezoneOffset: -7,  // (dup of destTimezoneOffset — ignore)
    travelMinutes: 242,             // flight time
    travelMinutesTotal: 242,        // including connections
    mileageActual: 1500,            // MileagePlus earnable miles
    serviceClassCountLowest: -1,    // -1 = not computed (?)
    bookingClassAvailability: "J4|JN4|C2|D1|Z0|ZN0|...|Y9|..|L9|...|X9",
                                    // pipe-separated RBD|seats pairs
    connections: [],                // nonstop = empty; otherwise array of flight dicts
    messages: [],
    warnings: [],
    stopInfos: [],
    mealBusinessFirst: "...",       // meal description
    mealCoach: "...",
    mealPremiumEconomy: "...",
    equipmentDisclosures: { ... },  // aircraft type + door specs
    hash: "...",                    // unique hash for dedup
    products: [                     // FARES — one per fare family, each potentially nested
      {
        productId: "O2UlRG74cACNZbvT8PQ8JB001",  // ← bookingToken — use this to select
        bookingCode: "N",           // RBD letter
        cabinType: "Coach",
        cabinTypeCode: "UE",
        columnId: 1,
        fareFamily: "ECO-BASIC",
        productSubtype: "BASE",
        productType: "ECO-BASIC",
        title: "United Economy",
        subTitle: "Basic (Most restrictive)",
        fares: [ { fareBasisCode: "LAA0AQBN" } ],
        prices: [
          { pricingType: "Fare",      amount: 210, amountBase: 209.41, currency: "USD" },
          { pricingType: "referencePrice", amount: 180.47, currency: "USD" },
          { pricingType: "Taxes",     amount: 29,  amountBase: 28.94, currency: "USD" },
          { pricingType: "saleFareTotalPrice", amount: 180.47 }
        ],
        mealDescription: "Meals for purchase",
        nonChangeableIndicator: true,
        isElf: true,              // Basic Economy flag
        isFareInBudget: true,
        isCabinInPolicy: true,
        isNestedParent: true,
        cabinGroupId: 1000,
        marketedCabins: ["UE"],
        nestedProducts: [         // UPSELL OPTIONS for the same flight+cabin
          { productId: "...005", productType: "ECONOMY", title: "United Economy",
            subTitle: "Standard", prices: [{pricingType:"Fare", amount:260, ...}], ... }
        ]
      },
      // ... more products for higher cabins (Economy Plus, First) ...
    ]
  }
}
```

**To select a flight**: grab the `productId` from the chosen product. That's
the booking token the next step needs. The `cartId` from the `meta` event
threads the session. The `hash` dedupes the same flight appearing in
multiple results.

### Captured example: Joe's search

File: `.captures/search-body.txt` — full SSE stream for AUS→SFO
Apr 28 Economy (including Basic), 1 passenger, 418KB, 32 flight options.

**The 1:00 PM flight from the screenshot = UA 1336, option #2 in stream:**
- UA 1336 AUS→SFO, depart 13:00, arrive 15:02 local, 4h02, 737-900 / B738
- Basic Economy: $210 (N class)
- Standard Economy: $260 (L class)
- (plus Economy Plus, First at higher prices; nested in products[0].nestedProducts)

## MileagePlus / membership activity (not yet captured)

Joe's framing: this is "membership activity" not "flight activity" — miles
are a currency of the MileagePlus membership; not every miles transaction
came from a flight (credit card earnings, partner hotels, etc.). TBD when
user navigates to that view.

## TODO

- [ ] Return slice search (Index=2) — need to select an outbound first, then
      capture. Same endpoint, different body.
- [ ] Flight-select / pricing call — converts `productId` → priced cart.
- [ ] Seat map endpoint
- [ ] Traveler details submission (names, KTN, FF#)
- [ ] Payment endpoint
- [ ] PNR creation confirmation (where does the record locator come back?)
- [ ] Award search (`AwardTravel: true`)
- [ ] Past trips / flight history — URL guess 404'd. Will capture when user
      finds the right UI control.
- [ ] MileagePlus miles activity (transactions)
- [ ] Boarding pass / check-in endpoints
- [ ] PNR lookup by record locator (for non-MileagePlus trips)

## Data provenance (from profile endpoint)

`/xapi/myunited/User/profile` → `.data.profile`:

| Field | Value example |
|-------|---------------|
| `CustomerId` | `53955798` |
| `ProfileId` | `19218370` |
| `ProfileOwnerId` | `53955798` (self-profile) |
| `ProfileMembersCount` | null (no linked travelers yet) |
| `Travelers[0].CustomerId` | `53955798` |
| `Travelers[0].MileagePlusId` | `XX118941` |
| `Travelers[0].TitleCode` | `Mr.` |
| `Travelers[0].CustomerName` | `Mr. Giuseppe Efisio Contini` |
| `Travelers[0].FirstName` | `Giuseppe` |
| `Travelers[0].MiddleName` | `Efisio` |
| `Travelers[0].LastName` | `Contini` |
| `Travelers[0].CustomerMetrics.PTCCode` | `PPR` (passenger type: Paid PRemier? or "Per Person Request") |
| ... | (more fields: addresses, phones, elite status) |

## Data provenance (balances)

`/api/myunited/user/balances` → `.data.Balances[]`:

| ProgramCurrencyType | Meaning |
|---------------------|---------|
| `RDM` | Redeemable miles (regular MileagePlus balance) |
| `UGC` | United gift card / unified ?gift credits |
| `UBC` | United travel Bank Credits |
| `CAC` | ?Certificate CAC |
| `PPE` | Plus Points Exchange |
| `FQP` | FlexPay / Flex Qualifying Points |
| `CHC` | Choice balance |
| `PQP` | Premier Qualifying Points (current year) |
| `PQF` | Premier Qualifying Flights |

And `.data.Certificates.Chase[]`: `CAV`, `CED`, `CEG`, `CBV`, `CAC` —
Chase-issued certificates for MileagePlus credit cardholders.

## Write endpoints (not yet captured)

Booking a flight, checkout, seat selection, check-in, etc. — capture in a
separate session when we actually book something.

## Known quirks

- **Akamai sensor POSTs:** randomized paths (`/favQ5fU6ptzCHIs0gPYD/...`),
  obfuscated bodies. Must pass through; don't drop.
- **`NumberOfItineraries: 0`** legitimately returns `{Data: [], EData: []}` —
  empty is valid, doesn't imply error.
- **`X-Authorization-api`** header name is case-sensitive in some places
  (`X-` capitalization). Safest to match the browser exactly.
- **`credentials: 'include'`** required on fetch — `SameSite` on cookies.
- **`expiresAt`** is ISO-8601 with 7 fractional second digits + `+00:00`
  offset.

## Tools for ongoing capture

- `core/bin/browse-capture.py` — the standard RE toolkit CDP capturer.
- `.captures/capture.py` — local helper that captures via CDP (request +
  response bodies, per-session JSONL). Idle mode for manually driving a
  user flow.
- Chunks pulled to `.captures/chunks/` for static analysis. Main chunk:
  `main.b74efa9bde4258f132bb.js` (~5.6MB).

## Discovered strings (actions/constants)

From `main.b74efa9bde4258f132bb.js`:

- Redux state key: `apiToken.hash` (Immutable Map)
- Action strings: `unitedapp/App/*` prefix
- Header constant: `"X-Authorization-api"` → `"bearer ".concat(e.hash)`
- Endpoints baked in: see table above
