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

## ⚠️ Akamai soft-block signature (learned the hard way)

When a POST to united.com returns **HTTP 200 + `Content-Type: application/x-ndjson` + `Content-Length: 0`**, this is **not** "malformed body". It's Akamai Bot Manager's **deception/tarpit action** silently dropping the response. Evidence:
- The response headers include `Server: volt-adc` (F5 Volterra edge), `x-accel-buffering: no` (stream wasn't buffered server-side — it's actually empty), and `Set-Cookie: akavpau_ualwww=...` (per-visitor auth cookie rotate — challenge signal).
- Malformed bodies return 400/500 with an error envelope.
- Reproduces even when **the same request is fired via `Runtime.evaluate` from inside the real Brave tab** (same JA4, same cookies, same everything).

**Implication for our skill**: don't replay POSTs against booking/state-change endpoints from Python urllib/http.client. Either:
1. Use `agentos.client` with `client="browser"` (bundles UA + Sec-CH-UA + Sec-Fetch-*); if the engine has wreq/BoringSSL support that's better still.
2. Drive the actual clicks via CDP on a live Brave session and **intercept** the XHR via `Fetch.enable` patterns — we read the real body the browser sent and the real response the browser got.

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

## Flight selection — **actually `RegisterFlights`, not `SelectAndFetch`**

I burned a lot of cycles assuming `/api/flight/SelectAndFetchSSENestedFlights`
was the "select outbound + search return" call. **It's not.** Live capture
of the actual "Basic Economy works for me → Select" click shows the SPA fires
**`POST /api/flight/RegisterFlights`** with a tiny body:

### Endpoint

```
POST https://www.united.com/api/flight/RegisterFlights
X-Authorization-api: bearer <hash>
Content-Type: application/json
```

### Request body (captured live on UA 1336 $259 Basic click)

```json
{
  "CartId": "2D7A02F9-C94F-476A-ADE2-04EE5CACBAE0",
  "BBXCellId": "VAU4pbMyP90PjjnrkiUqEA006",
  "MoneyAndMilesOptionId": null,
  "BBXSolutionSetId": null,
  "flightHash": "118-1336-UA",
  "RequeryForUpsell": false,
  "CalendarFilters": {"Filters": {"PriceScheduleOptions": {"Stops": 1}}},
  "FareType": "Refundable",
  "BuildHashValue": "true",
  "Characteristics": [
    {"Code": "IsNewRTI", "Value": "true"},
    {"Code": "fsrQueryParam", "Value": "?tt=1&st=bestmatches&d=2026-04-28&...&idx=1&mm=0&cartId=undefined"}
  ]
}
```

### Key observations on selection

- **`BBXCellId` is NOT the `productId` from search results.** The search gave us `VAU4pbMyP90PjjnrkiUqEA002` (suffix `002`); the click sent `VAU4pbMyP90PjjnrkiUqEA006` (suffix `006`). **The suffix maps to the fare-column ID** (basic, standard, plus, first). So the frontend takes the product-hash prefix and concatenates the chosen fare column. This needs confirming by capturing different fare clicks — TBD.
- **`flightHash` = `118-1336-UA`** — matches the `hash` field in the `flightOption` event.
- **`CartId` is the one from the SSE `meta` event** — thread from search → register.
- **`Characteristics.fsrQueryParam`** is the whole `?tt=1&...` URL query string from the choose-flights page. Frontend literally forwards the URL query. Possibly signal.
- **`IsNewRTI: "true"`** — probably "use the new Review Trip Itinerary UI". Safe to always send true.

### Response shape

`RegisterFlights` returns a **non-streaming JSON** envelope:

```
{
  "Data": {
    "CallTimeDomainFltRes": "63912572352.03",
    "CartId": "<same UUID>",
    "LastBBXSolutionSetId": "0G3SuIycAzpAM0zwGvgSflU",
    "Status": 1,
    "DisplayCart": {
      "CartId": "...",
      "GrandTotal": 308.4,             // base + taxes
      "SearchType": 1,
      "DisplayTravelers": [{...}],     // currently just logged-in user's DOB
      "DisplayTrips": [
        {
          "Origin": "AUS", "Destination": "SFO",
          "DisplayFlights": [ /* the selected UA 1336 segment */ ],
          "ColumnInformation": { "Columns": [...] }  // all fare columns with terms
        }
      ],
      "DisplayPrices": [...],
      "DisplayFees": [...],
      ...
    }
  }
}
```

**Round-trip**: even though our search was `tt=1` (round-trip), the Register
response `DisplayTrips` only shows **1 trip** (AUS→SFO). Yet the URL
transitioned directly to `/traveler/choose-travelers?cartId=...&tqp=R`
(tqp=R = Round-trip query param). That's weird — **one Register commits the
outbound AND skips the return slice**. Two plausible explanations:
1. The "search" phase was actually one-way under the hood (despite `tt=1`)
2. There's an additional Register call for the return leg we didn't capture
   because it happened too fast (but we monitored 45s of intercept — we'd
   have caught it).

TBD: start a round-trip search fresh, carefully click outbound, observe.

### Sidecar calls fired alongside Register (same click)

These are display/upsell enrichment — the skill does NOT need to call them to book:

| Method | Path | Body summary | Purpose |
|--------|------|--------------|---------|
| GET | `/xapi/myunited/User/profile` | — | (re-fetch profile, probably to show miles earning preview) |
| POST | `/api/flight/GetSpecialMealsEligibility` | Full FlightSegment dicts for each segment | Meal ordering eligibility per segment |
| POST | `/api/Flight/GetProducts` (note capital F) | `{CartId, ProductCodes: ["BAG"], Characteristics: [{Code: "OverrideBagPolicy", Value: "GeneralMember"}]}` | Baggage pricing/policy for current cart |
| POST | `/api/Flight/GetProducts` (second call) | `{CartId, ProductCodes: ["FLK"], ...}` | Flight change/cancel policy ("FLK" = flight ??) |

Both `GetProducts` calls return essentially the same 40KB cart snapshot.

## Post-selection — traveler page

URL pattern: `/en/us/traveler/choose-travelers?cartId=<UUID>&tqp=R`
(tqp=R = Round-trip; `tqp=O` would be one-way; `tqp=MC` multi-city).

**Note on `tqp`:** this URL param does NOT appear to actually toggle
round-trip vs one-way. A search fired with `tt=1` (alleged round-trip)
completed `RegisterFlights` and advanced to this page with `tqp=R` — but
`SearchType: 1` in the cart, and only 1 DisplayTrip, and the price
panel says "ONEWAY (1 TRAVELER)". Either `tt=1` means something else, or
the search was implicitly downgraded to one-way because the return date
never entered the Register call. **Round-trip booking path still TBD.**

### Form state (captured from `/en/us/traveler/choose-travelers`)

Form name: `rtiTraveler.travelers[i].*`. One row per traveler.

| Field name                                                    | Example value            |
|---------------------------------------------------------------|--------------------------|
| `travelers[0].travelerSelectedIndex`                          | `0` (0 = self, 1 = Priyanka, -1 = new) |
| (frequent flyer program select — opaque GUID name)            | `7 XX118941` (MP#, program 7 = United) |
| `travelers[0].extraDetails.phone.countryCode`                 | `1|US`                    |
| `travelers[0].extraDetails.phone.mobileNumber`                | `5126793195`              |
| `travelers[0].email`                                          | `united@contini.co`       |
| `travelers[0].extraDetails.travelerNumbers.knownTravelerNumber`| `158825994` (pre-populated) |
| `travelers[0].extraDetails.travelerNumbers.redressNumber`     | (empty)                   |
| `travelers[0].specialTravelNeed.wheelChair.isSelected`        | (checkbox)                |
| `travelers[0].specialTravelNeed.specialRequest.BSCT/BLND/DEAF/DPNA_1` | (checkboxes)       |
| `travelers[0].specialTravelNeed.serviceAnimal.isSelected`     | (checkbox)                |
| `confirmationSave-0`                                          | Save to MP profile? (checkbox) |

### Saved travelers dropdown

`/xapi/myunited/User/profile` → `data.profile.Travelers[]` holds saved
travelers under the same account. Each has its own `CustomerId` (numeric,
stable) even though they share a `MileagePlusId` (the profile owner's).
The dropdown maps the array index onto `rtiTraveler.travelers[i].travelerSelectedIndex`.

Captured for Joe (2 travelers):
- `CustomerId: 53955798, MP: XX118941` — Mr. Giuseppe Efisio Contini, DOB 1987-01-25, PTCCode `PPR`
- `CustomerId: 179221772, MP: XX118941` — Priyanka Raina, DOB 1992-02-15, PTCCode null

**Per-traveler Traveler object** from `/xapi/myunited/User/profile.data.profile.Travelers[i]`:
```
{
  CustomerId, MileagePlusId, TitleCode, CustomerName,
  FirstName, MiddleName, LastName, BirthDate ("1987-01-25T00:00:00"),
  GenderCode, CountryOfResidence, EliteDetails: {EliteStatus, Tier},
  CustomerMetrics: {PTCCode, ...},
  Addresses: [...], EmailAddresses: [...], CreditCards: [...],
  AirPreferences, DisplayPreferences, BehaviorSegments, Donor,
  HistoricalPartnerCards, ...
}
```

### `POST /api/ShoppingCart/RegisterTravelers` (the submit endpoint)

Captured live. Request body:

```jsonc
{
  "Channel": "WEB",
  "PetTravelers": null,
  "Travelers": null,        // LEGACY; the filled one is FlightTravelers
  "WorkFlowType": 1,
  "IsUMNROptIn": false,     // unaccompanied minor
  "FlightTravelers": [
    {
      "OxygenFlowRate": 0,
      "TravelerNameIndex": "",
      "Traveler": {
        "Person": {
          "Surname": "Contini", "GivenName": "Giuseppe", "MiddleName": "Efisio", "Suffix": "",
          "DateOfBirth": "01/25/1987",   // MM/DD/YYYY
          "Sex": "M",                     // Sex from GenderCode
          "Documents": [                  // KTN lives here
            {
              "DateOfBirth": "01/25/1987", "KnownTravelerNumber": "158825994",
              "RedressNumber": null, "CanadianTravelNumber": null,
              "GivenName": "Giuseppe", "MiddleName": "Efisio", "Surname": "Contini", "Suffix": "", "Sex": "M",
              "Type": 15                  // 15 = Secure Flight / KTN passenger doc
            }
          ],
          "CountryOfResidence": {},       // left empty — profile's "SG" is stale
          "Nationality": [],              // left empty for same reason
          "Type": "ADT",                   // Adult
          "InfantIndicator": "false",
          "Contact": {
            "Emails": [{ "Address": "united@contini.co" }],
            "PhoneNumbers": [{
              "Description": "H",          // H = Home/primary
              "CountryAccessCode": "US",   // NOTE: country NAME code
              "AreaCityCode": "1",         // NOTE: this holds the country calling code (+1)
              "PhoneNumber": "5126793195"  // 10-digit US number, no punctuation
            }]
          }
        },
        "LoyaltyProgramProfile": {
          "LoyaltyProgramCarrierCode": "UA",
          "LoyaltyProgramMemberID": "XX118941",
          "LoyaltyProgramID": "7",
          "LoyaltyProgramMemberTierLevel": null
        }
      },
      "SpecialServiceRequests": [],
      "PtcList": null
    }
  ],
  "SpecialServiceRequest": null,
  "IsReserved": false,
  "Characteristics": [
    {"Code": "OMNICHANNELCART", "Value": true},
    {"Code": "fsrQueryParam", "Value": "?tt=1&st=bestmatches&d=2026-04-28&...&idx=1&mm=0&cartId=undefined&pst=NXo%3D-G-C"}
  ],
  "CartId": "2D7A02F9-C94F-476A-ADE2-04EE5CACBAE0",
  "IsSessionFirst": false,
  "ReEvaluateExpressCheckout": false
}
```

Weird names to note:
- `AreaCityCode` actually holds the country calling code (`1`), not the area code
- `CountryAccessCode` holds the ISO country name (`US`), not an int
- `Documents[].Type: 15` = Secure Flight / KTN doc type per United's internal enum

### Endpoints that fired alongside / after RegisterTravelers

```
GET  /api/ShoppingCart/LoadReservationAndCart?cartId=<UUID>
GET  /xapi/myunited/User/profile  (re-fetch)
POST /api/User/ElectronicTravelCertificates?toCurrencyCode=USD
GET  /api/User/FutureFlightCreditsResiduals
```

The page then navigates to
`https://www.united.com/en/us/book-flight/customizetravel/<CartId>?tqp=R`
— the "customize travel" / upsell + seats + ancillaries page.

### Other booking endpoints (to capture on next pages)

From the bundle's URL inventory at `.captures/chunks/main.b74efa9bde4258f132bb.js`:

- POST `/api/Flight/SelectedFlights`
- POST `/api/Flight/FetchUpsell`
- POST `/api/ShoppingCart/RegisterOffers`
- POST `/api/ShoppingCart/RegisterSeats` (if seats picked)
- POST `/api/ShoppingCart/checkout`

## Customize travel — bundle offers

URL: `https://www.united.com/en/us/book-flight/customizetravel/<CartId>?tqp=R`

Reached after `RegisterTravelers`. Shows "Travel add-ons" with 3 bundle
cards (sometimes fewer/more). **No separate API fires to fetch bundles** —
the bundle data is already in `/api/ShoppingCart/LoadReservationAndCart`'s
response, but buried in a path the grep for "bundle"/"offer"/"merch"
didn't hit. The SPA hydrates React state from there.

**How to extract bundles from the page directly** (bypassing the
LoadReservationAndCart parse entirely — the React `bundleOffers` prop
has the clean normalized shape):

```js
// In the page context — via Runtime.evaluate:
(async () => {
  const h3 = Array.from(document.querySelectorAll('h3')).find(h => /Bundle Offer 1/i.test(h.textContent));
  let root = h3.parentElement;
  const fk = Object.keys(root).find(k => k.startsWith('__reactFiber$'));
  let fiber = root[fk];
  while (fiber) {
    if (Array.isArray(fiber.memoizedProps?.bundleOffers)) {
      return fiber.memoizedProps.bundleOffers;
    }
    fiber = fiber.return;
  }
})()
```

**bundleOffers prop shape** (verified live for AUS→SFO UA 1336):

```jsonc
[
  {
    "code": "B01",                    // bundle code
    "isBundle": true,
    "isPopularBundle": false,         // "Most Popular" banner driver
    "startingFromAmount": 76,         // base price shown in UI ($76 plus tax)
    "currencyCode": "USD",
    "allFlightsStartingFrom": null,
    "hasTax": true,
    "isIncluded": false,
    "partialEligibleProducts": [],
    "showAveragePricing": false,
    "isCovidWaiverDisabled": false,
    "content": {...}, "presentation": {...},  // i18n strings + icons
    "subProducts": [
      {
        "id": "1", "code": "B01", "groupCode": "BE", "subGroupCode": "B1",
        "name": "Economy Plus",       // what the card shows
        "associations": {
          "SegmentRefIDs": ["1"],     // 1 segment (outbound)
          "TravelerRefIDs": ["0"],
          "ODMappings": [{"SegmentRefIDs":["1"],"RefID":"OD1"}]
        },
        "prices": [
          {
            "id": "B1-SOL1_OD1_1_0",  // the BUNDLE OFFER ID used on select
            "amount": 81.7,           // WITH tax
            "baseAmount": 76,         // WITHOUT tax
            "currencyCode": "USD",
            "taxes": [{"type":"FET","code":"US","amount":5.7,"description":"U.S. Transportation Tax"}],
            "type": "Money",          // vs "Miles"
            "hasTax": true,
            "isIncluded": false,
            "subGroupCode": "B1",
            "characteristics": [{"Code":"RFIC","Value":"A"}]
          }
        ],
        "extension": { "Bundle": {  // full bundle contents
          "Products": [
            {"Code":"EPU", "SubProducts":[{"Descriptions":["E+ Ltd Recline Exit Middle"], ...}]}
          ]
        }}
      }
    ]
  },
  {
    "code": "B14",
    "isPopularBundle": true,
    "startingFromAmount": 104,
    "subProducts": [{
      "name": "Economy Plus and Extra Bag",
      "prices": [{"id": "B14-SOL1_OD1_1_0", "amount": 109.4, "baseAmount": 104, ...}]
    }]
  },
  {
    "code": "B18",
    "isPopularBundle": false,
    "startingFromAmount": 97,
    "subProducts": [{
      "name": "Economy Plus and Priority Boarding",
      "prices": [{"id": "B18-SOL1_OD1_1_0", "amount": 102.55, "baseAmount": 97, ...}]
    }]
  }
]
```

Bundle naming: `B01` = Economy Plus seat alone. Higher codes combine
products. `"B14"` with **Most Popular** flag is the 2-in-1 (seat + bag).
`B18` pairs seat + priority boarding.

Inputs on the page are named `check-offer-OD1` with values `0/1/2` (indices
into the bundleOffers array). Selecting one **probably** fires
`POST /api/ShoppingCart/RegisterOffers` with the price `id`
(`B14-SOL1_OD1_1_0` etc.) — **TBD: capture on click**.

## Flight-search aliases (from bundle)

A fuller list of flight-related endpoints discovered in main.js:

```
/api/Flight/SelectedFlights         # current selection detail
/api/flight/ApplyTravelCredits
/api/flight/FetchAwardCalendar
/api/flight/FetchCalendarFareMatrix
/api/flight/FetchFareColumnEntitlement
/api/flight/FetchFareWheel
/api/flight/FetchFlexibleCalendars
/api/flight/FetchFlights
/api/flight/FetchLmxQuotes              # hover/display: miles-earnable quotes per product (captured)
/api/flight/FetchMoneyAndMilesOptions
/api/flight/FetchSSENestedFlights       # THE search endpoint
/api/flight/FetchSessionFareWheel
/api/flight/FlightAmenitiesIndicator
/api/flight/GetCarbonEmissions
/api/flight/GetFareColumns
/api/flight/GetSpecialMealsEligibility  # (captured - fired alongside Register)
/api/flight/GetTeaserTexts
/api/flight/NestedCabinEntitlements
/api/flight/OnTimePerformanceInfoMulti
/api/flight/RecommendedFlights
/api/flight/RegisterFlights             # THE selection endpoint
/api/flight/RemoveTravelCredits
/api/flight/SelectAndFetchFlights       # NOT used; bundle still references it (dead?)
/api/flight/SelectAndFetchSSENestedFlights  # NOT used; silent 200+0byte = Akamai soft-block?
/api/flight/ValidateOADisservice
/api/flight/recentSearch
```

## MileagePlus / membership activity (not yet captured)

Joe's framing: this is "membership activity" not "flight activity" — miles
are a currency of the MileagePlus membership; not every miles transaction
came from a flight (credit card earnings, partner hotels, etc.). TBD when
user navigates to that view.

## TODO

- [x] Flight-select / pricing call — `RegisterFlights` is the one, not `SelectAndFetchSSENestedFlights`.
- [ ] Confirm round-trip: does the return slice need a second Register, or does the URL `tqp=R` param auto-book return at some later step?
- [ ] BBXCellId construction — capture clicks on different fare columns (Standard, First) to see how the suffix changes.
- [ ] Seat map endpoint (/api/ShoppingCart/RegisterSeats?)
- [ ] Traveler details submission (names, KTN, FF#, contact info) — NEXT capture
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
- **Checkout-flow idle timeout signs you out.** Sitting on the
  `/customizetravel/<CartId>` page for ~5min while NOT clicking things
  causes United to fire `GET /api/auth/signout` (clearing `AuthCookie`,
  `User`, `SID`) and returns 403 on the subsequent
  `/api/ShoppingCart/LoadReservationAndCart`. The cart survives (URL still
  has the cart ID), but the session is cooked. **The skill MUST run the
  booking flow (register → traveler → offers → seats → checkout) without
  long pauses.** Inspection/probing must happen either before starting,
  or after PNR generation.

## Tools for ongoing capture

- `core/bin/browse-capture.py` — the standard RE toolkit CDP capturer.
- `.captures/capture.py` — local helper that captures via CDP (request +
  response bodies, per-session JSONL). Idle mode for manually driving a
  user flow. **Caveat:** this script flushes on clean exit only — Ctrl-C
  during the idle pump loses data. Use `/tmp/fix-capture.py` for
  flush-on-each-event behavior.
- **`/tmp/united-click-and-capture.py`** — the winning pattern: attach to
  Brave, enable `Fetch.enable` interception for `/api/flight/*` + `/xapi/*`,
  drive clicks via `Runtime.evaluate`, drain SSE streams via
  `Fetch.takeResponseBodyAsStream` + `IO.read` loop, fulfill requests so
  the browser keeps working. **This is the reliable way to capture
  state-change XHRs** — no SSE buffering issues, no TLS fingerprint
  mismatch, bodies always intact.
- Chunks pulled to `.captures/chunks/` for static analysis. Main chunk:
  `main.b74efa9bde4258f132bb.js` (~5.6MB).

## Discovered strings (actions/constants)

From `main.b74efa9bde4258f132bb.js`:

- Redux state key: `apiToken.hash` (Immutable Map)
- Action strings: `unitedapp/App/*` prefix
- Header constant: `"X-Authorization-api"` → `"bearer ".concat(e.hash)`
- Endpoints baked in: see table above

## Seatmap (post-traveler-page)

URL pattern: `/en/us/book-flight/seatmap/<CartId>?tqp=R`

### `POST /api/SeatMap/Retrieve`

Fires on seatmap page load. Returns full aircraft cabin data with every
seat, pricing tier, monuments (galley/lavatory/exit), and wing/exit row
flags. Response is ~300KB+ of rich JSON.

Request body (captured live):

```jsonc
{
  "cartId": "<UUID>",
  "channelTransactionId": "<UUID>",
  "reservationReferenceId": "<same as cartId>",
  "correlationId": "",
  "sessionKey": "<cartId><UUID>",      // session-bound handle
  "dodCabins": ["J", "O"],             // cabin codes to retrieve
  "seatMapRequest": {
    "recordLocator": null,
    "recordLocatorCreatedDate": null,
    "languageCode": "en-US",
    "isLapChild": false,
    "isAwardReservation": false,
    "flightSegments": [{
      "premiumProducts": [],
      "arrivalAirport": {"iataCode":"SFO","iataCountryCode":{"CountryCode":"US"}},
      "arrivalDateTime": "2026-04-28T15:02",
      "checkInSegment": false,
      "classOfService": "N",               // RBD from selected fare
      "coupons": [{}],
      "departureAirport": {"iataCode":"AUS","iataCountryCode":{"CountryCode":"US"}},
      "departureDateTime": "2026-04-28T13:00",
      "farebasisCode": "LAA0AQBN",        // from selected fare
      "flightNumber": 1336,
      "isValidSegment": true,
      "marketingAirlineCode": "UA",
      "operatingAirlineCode": "UA",
      "operatingFlightNumber": 1336,
      "pricing": "true",
      "segmentNumber": 1
    }],
    "lofSegments": []
  }
}
```

### Response shape (the important parts)

```jsonc
{
  "transactionIdentifiers": {"transactionId": "..."},
  "softErrors": [],
  "flightInfo": {
    "marketingFlightNumber": 1336,
    "operatingFlightNumber": 1336,
    "marketingCarrierCode": "UA",
    "operatingCarrierCode": "UA",
    "departureDate": "2026-04-28T13:00:00",
    "departureAirport": "AUS",
    "arrivalAirport": "SFO",
    "noSeatSelectionWindow": false
  },
  "aircraftInfo": {
    "tailNumber": null,
    "icr": "C3E"                 // aircraft config code, not registration
  },
  "cabins": [
    {
      "isUpperDeck": false,
      "cabinType": "J",            // J=First, Y=Economy
      "cabinBrand": "United First",
      "cabinBranded": "United First{R}",
      "layout": "AB EF",           // letters per side, space = aisle
      "rowCount": 4,
      "columnCount": 5,             // includes the aisle column
      "availableSeats": 0,
      "totalSeats": 16,
      "rows": [
        {
          "number": 1,
          "verticalGridNumber": 1000,  // sortable with monumentRows
          "wing": false,
          "seats": [
            {
              "number": "1A", "letter": "A", "rowNumber": 1,
              "tier": "1",                 // price-tier reference
              "location": "Window",         // "Window" | "Middle" | "Aisle"
              "seatSection": "Left",        // Left | Right
              "itemType": "SEAT",           // or "MONUMENT"
              "description": null,          // e.g. "Economy Plus", "Preferred Zone"
              "isAvailable": false,
              "isBlocked": false,
              "isPermanentBlocked": false,
              "isOccupied": true,
              "isExit": false,              // this SEAT is an exit-row seat
              "isDoorExit": false,
              "isOnWing": false,
              "isBulkhead": true,           // first row of cabin
              "isExtraPitch": true,
              "isExtraSeatWidth": true,
              "hasInSeatPower": false,
              "isWindowObstructedView": false,
              "isLimitedSeatWidth": false,
              "hasNoUnderSeatStorage": false,
              "sellableSeatCategory": "...",
              "iataAttributes": [...]       // IATA-standard attribute codes
              // ... plus ~40 more per-seat flags (isBassinet, isHeld,
              // allowPet, allowPrisonerGuard, allowDisabledPassenger,
              // allowLapInfant, allowUnAccompaniedMinor, ...)
            }
          ]
        }
      ],
      "monumentRows": [              // items BETWEEN seat rows (sorted by verticalGridNumber)
        {
          "verticalGridNumber": 2,
          "monuments": [{
            "itemType": "SPACER",     // "AISLE" | "SPACER" | "GALLEY" | "LAV"
            "isDoorExit": true,        // marks a door
            "horizontalGridNumber": 1,
            "horizontalSpan": 1,
            "verticalSpan": 1
          }]
        }
      ]
    }
  ],
  "tiers": [                         // pricing tiers
    {
      "id": 1,
      "currencyCode": "USD",
      "numberOfDecimals": 2.0,
      "pricing": [{
        "basePrice": 0, "totalPrice": 0,
        "eligibility": "Prime Business Seats are not eligible",
        "pricingValidators": [...]    // opaque per-seat validator tokens
      }]
    }
    // tier 2..7 with actual prices; tier 8..19 = "Seat selection not eligible for ELF Fare" (Basic Economy)
  ],
  "travelers": [...]
}
```

### Rendering a cabin from this data

Merge `rows` + `monumentRows` into one sorted list by `verticalGridNumber`.
Walk in order — seat rows emit seats indexed by letter; monument rows emit
headers ("LAVATORY", "GALLEY", "DOOR/EXIT" if `isDoorExit: true`).

Example ASCII render for UA 1336 (captured 2026-04-23):

```
╔════  Cabin 0: United First  (0/16, layout AB EF)  ════
║  ROW     A  B │ E  F
║  ░░░     ──  LAVATORY  ──
║  ░░░     ══  DOOR / EXIT  ══
║  ░░░     ──  GALLEY  ──
║    1     ✕  ✕ │ ✕  ✕   ← BULKHEAD
║    2     ✕  ✕ │ ✕  ✕
║    3     ✕  ✕ │ ✕  ✕
║    4     ✕  ✕ │ ✕  ✕

╔════  Cabin 1: United Economy  (19/138, layout ABC DEF)  ════
║  ROW     A  B  C │ D  E  F
║    7     ✕  ✕  ✕ │ █  █  ✕   ← BULKHEAD
║    8     ✕  ✕  ✕ │ ✕  ✕  ✕
║   10     ✕  ✕  ✕ │ ✕  ✕  ✕
║   ...
║   20     ✕  ✕  ✕ │ ✕  ✕  ✕   ← WING, EXIT-ROW
║   21     ✕  ✕  ✕ │ ✕  ✕  ✕   ← WING, EXIT-ROW
║   22     ✕  $  ✕ │ ✕  $  ✕   ← WING
║   ...
```

Legend: ✕=occupied, ○=free available, $=paid available, █=blocked, ·=no seat.

**Important rendering notes:**
- Rows are numbered non-contiguously (e.g. skip from 8→10, 12→14, 32→34).
  Missing numbers are airline convention (skip 13, skip 15-19 for 737
  config variant) — don't synthesize missing rows.
- `row.wing: true` marks rows physically over the wing.
- `seat.isExit: true` marks seats on an **exit row** (legal requirement:
  adult, able-bodied, etc.). `monument.isDoorExit: true` marks an actual
  aircraft door location between rows.
- `seat.isBlocked` = blocked by airline (e.g. middle rows held back as
  "elite only"). Shown as blocked squares in the UI.
- `tier` on a seat is a lookup key into `tiers[i].id`. `tiers[i].pricing[0]
  .totalPrice` is the per-seat charge (pre-tax).
- Tiers 8-19 (Basic Economy etc.) have `totalPrice: 0` and
  `eligibility: "Seat selection not eligible for ELF Fare"` — meaning the
  fare class doesn't allow paid seat selection at all.

### What determines a seat's availability

```
if not isAvailable:
  if isBlocked or isPermanentBlocked: "blocked"    (airline held back)
  elif isOccupied:                   "occupied"
  elif isHeld:                       "held"        (another traveler mid-checkout)
  else:                              "unavailable"
```

### Other seatmap signals
- `isReserveSeat`: airline-reserved seat (e.g. pilot jumpseat, crew rest)
- `isBassinet`: bassinet mount available at this seat
- `allowPet`, `allowLapInfant`, `allowUnAccompaniedMinor`,
  `allowDisabledPassenger`, `allowPrisonerGuard`: booking-rule flags
- `iataAttributes`: list of IATA standard attribute codes (e.g. `"1A"` =
  window, `"8"` = no seat recline) — gives portable cross-airline
  attributes even if United's own naming differs

### `aircraftInfo.icr`

ICR code (e.g. `C3E`) is United's internal aircraft configuration code.
Maps to aircraft type + cabin layout revision. Not the same as tail
number. For displaying "737-800" etc., cross-reference from the earlier
search response's `equipmentDisclosures.equipmentType`/`equipmentDescription`.

## Drive pattern that actually worked (lessons learned 2026-04-23)

After several false starts, this sequence worked end-to-end:

1. **Verify login via cookies first** (`Network.getCookies` for
   AuthCookie/User/SID present). If missing, the whole flow will fail
   silently on `LoadReservationAndCart` 403.
2. **Don't deep-link to `/fsr/choose-flights` cold** — the SPA may not
   kick off `FetchSSENestedFlights` if it thinks the state is already
   cached. Instead: navigate to `united.com/en/us` first, then to the
   `/fsr/choose-flights?f=AUS&t=SFO&d=...&r=...` URL (round-trip URL
   auto-fires search).
3. **Round-trip URL renders `Select flight` buttons, not `Select a fare`**
   — the matrix UI for round-trip shows price cards per (flight × cabin).
   Click the `$<price>` button inside the row for the target flight.
   One-way URLs (`tt=0`) caused "unable to complete your request" on our
   session, so stick with round-trip URL even if you only want one-way;
   the Register call will treat it as one-way based on body.
4. **Scope button finders to the UA row ancestor** — `document.querySelectorAll('button')`
   globally finds 30+ unrelated buttons. Walk up from a DOM leaf with
   "UA <number>" text until you find a Flight container, then `querySelectorAll`
   within it.
5. **After Select fare → "Basic Economy works for me" toggle → Select**
   — the `Select` button is disabled until the toggle is clicked (on
   Basic Economy fares only).
6. **Each page fires different endpoints** — `RegisterFlights` on the
   Select click, `RegisterTravelers` on traveler-page Continue,
   `SeatMap/Retrieve` on seatmap page load. Don't pause between clicks;
   the session idles out after ~5min.
7. **`Fetch.enable` breaks SSE** — only use passive `Network.enable`
   capture. Exclude SSE endpoints from any intercept list.

## Next session: capture round-trip booking (2026-04-23)

Everything above is one-way. The skill stops short of checkout, and it
only models a single `RegisterFlights` call for a single outbound
segment. To book a round-trip we need to extend the flow.

### Goal

Book the round-trip AUS↔SFO Joe picked originally:
- **Outbound** Tue Apr 28 UA 1336, 1:00 PM → 3:02 PM AUS→SFO (known, $210 Basic)
- **Return** Sun May 3, 5:10 PM → 10:55 PM SFO→AUS (nonstop, 3h 45m — UA flight number TBD; find via fresh search)

End-to-end via the skill, **using CDP to drive the UI** only where the
HTTP replay fails. Stop short of the final checkout POST (no payment).

### Start of session checklist

1. `boot()` via agentOS MCP to pick up state.
2. Verify Brave CDP: `curl -s http://127.0.0.1:9222/json`. If nothing
   on `united.com`, navigate to `https://www.united.com/en/us` and
   confirm login via `Network.getCookies` — `AuthCookie`, `User`, `SID`
   must all be present. If missing, ask Joe to log in. DO NOT try
   driving the flow while logged-out; everything 403s.
3. `check_session` via the skill — should return `united:XX118941`.
4. Read this file (requirements.md) tail. The Drive pattern that
   actually worked (2026-04-23) section is the known-good UI
   selectors.

### Hypothesis A: two RegisterFlights calls with shared CartId

Most airline booking APIs split the round-trip into two selection
calls. Try this first — the skill change is small:

1. `search_flights(origin=AUS, destination=SFO, depart_date=2026-04-28, return_date=2026-05-03)` — today the skill ignores `return_date`. Fix: fire one search for each slice. The search body's `Trips[]` takes one segment at a time anyway (we verified in the 2026-04-23 captures). For a round-trip session, fire search twice with `TripIndex: 1` for outbound and `TripIndex: 2` for return, sharing the `CartId` from the first search's `meta` event via `UsePassedCartId: true`.
2. `select_flight(cart_id, booking_token=<outbound>, flight_hash=<outbound>)` — same as today, but on success don't advance to traveler page. Inspect the `DisplayCart.SearchType` field — it should be 2 for round-trip (we saw 1 for one-way).
3. `select_flight(cart_id, booking_token=<return>, flight_hash=<return>, trip_index=2)` — NEW optional param. Probably maps to `TripIndex: 2` in the RegisterFlights body.
4. After both slices registered, the cart should show TWO DisplayTrips with a combined GrandTotal. Then `register_traveler`, `get_seatmap` twice (once per slice), `register_seats` optionally per slice.

### Hypothesis B: one RegisterFlights with two SelectedProducts

Less likely but possible — the body could carry an array of
`{ProductId, TripIndex}` entries. If Hypothesis A fails with a
"missing slice" error, try this body shape in a one-shot Register
call.

### Falling back to CDP capture

If both hypotheses fail or return confusing errors, **capture the
real frontend round-trip flow** with the same pattern used on 2026-04-23:

1. Start `python3 /tmp/united-intercept-safe.py 600 /tmp/rt-intercept.json` (or rewrite a fresh intercept script — it's <100 lines). **Exclude SSE endpoints** (FetchSSENestedFlights, SelectAndFetchSSENestedFlights) from the Fetch.enable patterns; passive Network.enable handles them.
2. Drive via `Runtime.evaluate`:
   - Navigate to `https://www.united.com/en/us/fsr/choose-flights?f=AUS&t=SFO&d=2026-04-28&r=2026-05-03&px=1&tt=1&taxng=1&clm=7&st=bestmatches&idx=1&mm=0` (round-trip URL already has `r=<return-date>` and `tt=1`).
   - Wait for flight cards. The round-trip UI shows `Select flight` buttons (NOT `Select a fare`). Scope to the UA 1336 row ancestor.
   - Click the cheapest `Select flight` → fare panel → "Basic Economy works for me" → `Select`.
   - This transitions the URL to `idx=2` for the RETURN slice. Wait for return-flight cards, pick the 5:10 PM return, repeat the select+toggle+select dance.
   - Record every XHR between the first and second Select click — that's the round-trip delta.
3. Diff the captured `RegisterFlights` body for the return slice against the outbound to see the `TripIndex` / `SelectedProducts` fields.

### Expected new fields / endpoints (speculative, verify on capture)

- `RegisterFlights` body: `TripIndex: 2` OR top-level `SelectedProducts: [{ProductId: <return>, TripIndex: 2}]` OR new endpoint.
- After both slices registered: URL probably jumps straight to `/traveler/` since the cart is complete.
- `get_seatmap` may need to be called TWICE — once per segment (different `SegmentNumber`).
- `register_traveler` is probably fine unchanged — the traveler is the same person for both legs.
- `register_seats` needs to handle `OriginalSegmentIndex` / `LegIndex` for the return-slice seat.

### Skill changes to anticipate

- `search_flights`: add `return_date` handling (fire two searches, merge offers).
- `select_flight`: add `trip_index` param (default 1); handle two-call sequences with shared CartId.
- `select_round_trip` convenience wrapper: takes outbound_token + return_token + cart_id + hashes, does both calls in order.
- `get_seatmap`: accept `segment_number` (already does); caller loops over segments for round-trip.
- Possibly new `get_cart` tool that calls `LoadReservationAndCart` and returns a normalized snapshot (total, selected trips, traveler state, seats) — useful for mid-flow status checks.

### Shape open question: how to model round-trip price breakdown

A round-trip `reservation` has ONE grand total but two `trips[]`. Each
leg has its own base fare; taxes often apply per-segment. The current
reservation shape has `totalAmount`/`baseAmount`/`taxAmount` at the
top level. For round-trip we may want `perTrip: {tripId: {base, tax, total}}`
in `_conditions` or as a new per-trip breakdown. Don't over-engineer
before we see the actual cart data.

### Stop before payment

The final `/api/ShoppingCart/checkout` or similar endpoint is
**out of scope**. Capture it for reference but DO NOT call it. Joe
will never put his card into this skill — if he wants to book for
real he does it in the browser. The skill's value is everything
up to the moment of charge.

### Free seats — quick win before round-trip

Joe asked: are any seats free on UA 1336? Answer from today's
captures: **no — all available seats have a paid tier on Basic Economy
fares.** That's United's Basic-Economy policy (tiers 8-19 are flagged
`eligibility: "Seat selection not eligible for ELF Fare"` with
`totalPrice: 0`, meaning "pick nothing, get assigned at check-in").
A paid non-Basic fare (Standard Economy $260+) unlocks free seat
selection in Main Cabin. Worth surfacing this in `get_seatmap`'s
return — add a `freeEconomySeatsAvailable: boolean` derived by
checking if any non-paid tier has `eligibility` matching "Eligible".

## Round-trip booking — what we actually learned (2026-04-23 session 2)

### The round-trip cart signal (open mystery)

Body-only HTTP replay cannot create a `SearchType: 2` (round-trip)
cart. We tried, in escalating combinations:

- `Trips[0].TripIndex: 2` + `UsePassedCartId: true` in the search body
- `Referer: .../fsr/choose-flights?...&r=<return>&sc=7,7&tt=1&...`
- `Characteristics[].fsrQueryParam` echoed in the RegisterFlights body
- `POST /api/FlexPricer/CalendarPricing` priming call with `IsOneway: false`
  **before** the search (this is what the SPA fires from the homepage
  when the user picks dates; it carries `Return: 05032026` and
  `Depart: 04282026` in the body)

All four together still produce `SearchType: 1` (one-way) carts. The
server-side bit that flips a cart to round-trip is **not reachable from
our HTTP client's vantage point** via any signal we've identified.
Suspects not yet ruled out:

- A session-scoped server-side attribute set during the homepage nav
  itself (a cookie, or a server-side session var).
- The `pst=NXo%3D-G-C` param in the round-trip choose-flights URL —
  this is "page state token", opaque, likely has the round-trip flag
  baked in via HMAC or similar.
- Ordering: the SPA fires `CalendarPricing` *before* the navigation
  POST to `/en/us`, not after.

What DOES work: drive the SPA via CDP from the homepage (click the
round-trip radio, fill origin/destination/dates via `setDepart`/`setReturn`
on the datepicker fiber, click "Find flights"). The resulting cart
becomes round-trip. Everything else — `select_flight`, `register_traveler`,
`get_seatmap`, `get_cart` — works pure-Python against that cart.

### The two-variant search body

The SPA uses two different FetchSSENestedFlights body shapes:

- **Outbound (idx=1)**: `SearchTypeSelection: 1` with full `Trips[0]`
  (origin, destination, depart date, fare family). Standard shape.
- **Return (idx=2, existing cart)**: `SearchTypeSelection: 3` with
  `Trips: []` (empty). The server fills in the slice from the cart's
  committed outbound. `RecentSearchKey` must be empty.

`search_flights` now emits the type-3 variant when `cart_id` is passed
with `trip_index >= 2`. It's necessary for the return search to stay on
the same cart — a type-1 return search allocates a fresh cart instead.

### CalendarPricing priming endpoint

```
POST /api/FlexPricer/CalendarPricing
Referer: https://www.united.com/en/us
{
  "UserSelected": true,
  "Depart": "04282026",          // MMDDYYYY
  "Return": "05032026",          // MMDDYYYY
  "Origin": "AUS",
  "Destination": "SFO",
  "IsAward": false,
  "ClientCurrentDate": "",
  "IsPremium": false,
  "IsOneway": false,             // ← the one that looks like it should matter
  "ExcludeBasicEconomy": false,
  "Travelers": { "Adult": 1, "Senior": 0, "Infant": 0, "InfantOnLap": 0,
                 "Children01": 0, "Children02": 0, "Children03": 0, "Children04": 0 }
}
```

Fires from homepage when dates are picked. We wired `search_flights`
to call this before the actual flight search when `return_date` is
set — doesn't flip SearchType on its own but matches the SPA's call
chain, which is probably load-bearing for other things.

### LoadReservationAndCart requires SPA's query params

Calling `/api/ShoppingCart/LoadReservationAndCart?cartId=<UUID>` with
just `cartId` returns a generic `{"Error":{"Errors":[{"Message":"We're
sorry, we are having connection issues. Please try your transaction
again."}]}}`. The SPA sends additional query params the server expects:

```
GET /api/ShoppingCart/LoadReservationAndCart
  ?cartId=<UUID>
  &workFlowType=1
  &clearBundles=false
  &clearSeats=false
  &isConfirmationPage=false
```

With those, it returns 200 with the full cart. Response envelope is
`Data.CartData.DisplayCart`, not `Data.DisplayCart` (different nesting
from RegisterFlights/RegisterTravelers responses — accept both in
parsing). This is wired in `get_cart`.

### Booking flow — complete page + endpoint map (2026-04-23)

Verified end-to-end on cart `76695CE3`. Page sequence AFTER a round-trip
cart is born (via SPA drive on homepage):

| # | URL / Endpoint | Trigger | What it does |
|---|---|---|---|
| 1 | `/fsr/choose-flights?...&sc=7,7` | Homepage form submit | Fires `CalendarPricing` (prime) + first `FetchSSENestedFlights` (outbound search). Cart is born in the meta event of the search. |
| 2 | click outbound fare → Basic toggle → Select | User click | POST `/api/flight/NestedCabinEntitlements` (prefetch), then POST `/api/flight/RegisterFlights` with `TripIndex:1`. URL transitions to `idx=2`. |
| 3 | `/fsr/choose-flights?...&idx=2&cartId=...` | Auto-nav from idx=1 select | Second `FetchSSENestedFlights` with `SearchTypeSelection:3` + empty Trips — server picks the return slice from cart context. |
| 4 | click return fare → Select | User click | POST `/api/flight/RegisterFlights` with `TripIndex:2`. URL transitions to `/traveler/choose-travelers?cartId=...&tqp=R`. |
| 5 | `/traveler/choose-travelers?cartId=...` | Auto-nav after 2nd Register | The SPA pre-fills traveler name/DOB/contact from `/xapi/myunited/User/profile`. Continue button submits `/api/ShoppingCart/RegisterTravelers`. Response includes full `DisplayCart` with `SearchType:2`, `GrandTotal`, both `DisplayTrips[]`. |
| 6 | `/book-flight/customizetravel/<cartId>?tqp=R` | Auto-nav after RegisterTravelers | "Travel add-ons" page — 3 bundle offers per slice. On Basic Economy you want to decline; the button is labeled **"Continue to seats"** (not "Skip" or similar). |
| 7 | `/book-flight/seatmap/<cartId>?tqp=R` | Auto-nav after bundles | Seat picker. For Basic Economy, seats are paid-only or skip. Skip proceeds to payment. |
| 8 | `/book-flight/payment/<cartId>?tqp=R` (next) | Seatmap next | Payment page. NOT YET CAPTURED. |
| 9 | POST `/api/ShoppingCart/checkout` (suspected) | Payment submit | NOT YET CAPTURED. |

### Critical UI selectors (for future CDP drive)

All validated 2026-04-23:

- **Homepage round-trip radio**: `input[name=flightType][value=roundTrip]` (pre-selected by default)
- **Datepicker React fiber trick**: walk fiber up from `#DepartDate` until you find a fiber whose `memoizedProps` has both `setDepart` and `setReturn` — call with native `Date` objects. Displays populate but form validation rejects the state, so prefer calendar clicks.
- **Calendar day cells**: inside `<table aria-label="April 2026">` etc. Cells past today have `aria-label=null` and text like `"28\n$373"`. `.click()` works.
- **Find flights submit**: `button[aria-label="Find flights"]` — after calendar clicks the state is valid, submit works.
- **Fare panel "From $XXX" button** for a flight: walk up from a text-node matching `^UA\s*<num>\s` until you find an ancestor with >3 buttons, then find `b.innerText.replace(/\s+/g,'') === 'From$XXX'`.
- **"Basic Economy works for me" toggle**: `label[aria-label="Basic Economy works for me."]` — required before the Basic Select button enables.
- **Basic Select button**: `button[aria-label="Select United Economy Basic (Most restrictive)"]`.
- **Round-trip return select**: only the first Basic select panel has the works-for-me toggle; on return slice it may be auto-enabled (seen once).
- **Continue (traveler page)**: `button[type=submit]` with text "Continue" whose ancestor is `#parentCommonShoppingCartContainer`. **`.click()` and real `Input.dispatchMouseEvent` BOTH failed to advance the page when we were RE-landing on the traveler page post-RegisterTravelers** — cart was already past that step server-side. Solution: navigate directly to `/book-flight/customizetravel/<cartId>?tqp=R`.
- **"Continue to seats"**: on customize-travel page. Plain `.click()` may or may not work; real `Input.dispatchMouseEvent` at element coords works reliably.

### Cart lifecycle observations

- Multiple carts can coexist per session. Each successful RegisterFlights
  on a fresh search mints a new cartId. Old carts idle until they expire
  (~5–15 min of inactivity observed; requirements.md:910 mentions ~5min
  but we held `76695CE3` for ~30min and it was still alive).
- `LoadReservationAndCart` returning the "connection issues" error ≠
  cart dead — it can mean the cart is missing query params OR the
  request needs a Referer. Dead-cart is a different response.
- `/traveler/` page sends `/api/auth/signout` if the user sits idle on
  it — that's what kills sessions, not the cart itself.

### Resume behavior

Re-navigating to `/traveler/choose-travelers?cartId=<cartId>` on a cart
that's already past RegisterTravelers simply re-renders the traveler
page with pre-filled data but **does not re-submit**. The Continue
button on re-landing does nothing because the cart state is already
past that step. To advance, navigate directly to the next page's URL
(`/book-flight/customizetravel/<cartId>`).

### Skip-the-browser hybrid (actually shipping)

Given the `SearchType:2` mystery, the realistic shape for a
pure-Python booking is:

1. `prepare_round_trip_cart(origin, dest, depart, return_date)` — uses
   CDP to drive the SPA homepage→choose-flights to create a round-trip
   cart. Returns the cart_id.
2. Everything else via pure HTTP:
   - `search_flights(cart_id=..., trip_index=1)` — get outbound offers for the active cart
   - `select_flight(..., trip_index=1)` — commit outbound
   - `search_flights(cart_id=..., trip_index=2)` — get return offers (type-3 body)
   - `select_flight(..., trip_index=2)` — commit return
   - `register_traveler(cart_id=...)`
   - `get_cart(cart_id=...)` — snapshot anytime
   - `continue_to_bundles(cart_id=...)` — no-op on our end (server auto-advances)
   - `skip_bundles(cart_id=...)` — call whatever endpoint the "Continue to seats" button fires. TODO capture.
   - `skip_seats(cart_id=...)` — call whatever endpoint the seatmap "skip" button fires. TODO capture.
   - `submit_payment(cart_id=..., payment_method_id=...)` — the final `POST /api/ShoppingCart/checkout`. TODO capture body.

Until those TODOs are captured, driving the last 2-3 clicks via CDP is
the pragmatic path. That's what we did this session.

## Session 2 discoveries (2026-04-23 evening)

### Full checkout page capture

Drove cart `76695CE3-015A-46F7-84DD-AFF523E427F3` end-to-end:
`/traveler/` → `/book-flight/customizetravel/<cartId>?tqp=R`
→ `/book-flight/seatmap/<cartId>?tqp=R` → `/book-flight/checkout/<cartId>?tqp=R`.

**Page-advance buttons** (verified working via `Input.dispatchMouseEvent`
at element coordinates; plain `.click()` sometimes fails when the button
isn't in a form):

| Page | Button text | Button aria / selector | Fires |
|---|---|---|---|
| `/traveler/` | "Continue" | `button[type=submit]` inside `#parentCommonShoppingCartContainer` | `/api/ShoppingCart/RegisterTravelers` (already captured) |
| `/customizetravel/` | "Continue to seats" | `button[type=submit]` matching innerText | Auto-advance to `/seatmap/` (no new backend call observed on skip) |
| `/seatmap/` | "Continue to checkout" | `button` matching innerText | Auto-advance to `/checkout/` |
| `/checkout/` | "Agree and purchase" | `button` containing `<span>Agree and purchase</span>` | **NOT YET CAPTURED** — POSTs to the final charge endpoint |

**Critical on checkout page**: after clicking Purchase, the insurance
offer appears **in-page** (not a new page). Must select "No, I will
not travel without this insurance" + click Next to unblock the
Purchase button. Joe clicked it live; the resolved state persists.

### Saved cards endpoint

**`GET /api/user/creditCards`** returns all saved cards on the
logged-in account. PCI-safe: last4, BIN, expiry, IATA 2-char code
(AX/VI/MC/DS/DC/TP/JC/UP), opaque handles. No PAN or CVV ever.

Captured shape (one card):
```json
{
  "CustomerId": 53955798,
  "CCTypeDescription": "American Express",
  "CustomDescription": "AMEX Platinum",
  "Code": "AX",
  "ExpMonth": 12, "ExpYear": 2027,
  "AccountNumberLastFourDigits": "1007",
  "AccountNumberMasked": "American Express  **1007",
  "PersistentToken": "473009498861007",
  "AccountNumberToken": "a66ced2d-1749-4b03-9326-e005a9bfe0a0",
  "Key": "0ATu79Wy0J4wgmp2iAaMYpAdnpVTDCVosnsOpFCVaMRFmV96+W2AOt+SLGMWhjklZ3Gu3McsCIA0CVO7u3ZnXs4cr/zkDNCsP9xIctZpc6s=",
  "AddressKey": "...",
  "ExpirationDate": "12/31/2027",
  "Payor": {"GivenName": "Giuseppe Contini"},
  "IsDefault": false, "IsPrimary": false, "IsSelected": false
}
```

Three cards on Joe's account:
- **2005** — American Express (no custom description) — currently default-selected on checkout
- **1007** — American Express (custom name "AMEX Platinum")
- **9768** — MasterCard (custom name "AncestryPass Debit")

Three opaque handles per card that thread into the checkout POST:
`Key`, `AccountNumberToken`, `PersistentToken`. Skill stores all three
in `payment_method.providerTokens`.

### Eligible forms of payment endpoint

**`POST /api/payment/GetEligibleFOP`** returns the payment method
TYPES eligible for a specific cart (VI, MC, AX, DS, DC, TP, JC, UP,
MPVI, PP, PZ, AP, TC). Request body mirrors the cart + passenger
shape. Response lists per-type rules: SortOrder, ForceReserveHours,
HoldTimeHours, TimeToLive. Doesn't pick the actual card — that's
still in the cart's selected-card state.

**`POST /api/FlexPricer/CalendarPricing`** — prime call fired from
homepage when dates are picked. Body carries `IsOneway: false` +
Depart/Return dates. Wired into `search_flights` when `return_date`
is set. Doesn't flip SearchType on its own (tested) but matches the
SPA's call chain.

**`POST /api/Payment/GetTermsAndConditions`** — checkout T&C text.
**`POST /api/Payment/GetCreditsFromSession`** — travel credits on account.
**`POST /api/Payment/IsPartnerProvisionEnabled`** — Chase/partner flags.
**`POST /api/Products/OfferDetail`** — bundle offer detail.
**`POST /api/CCEProducts/Coupons/get-coupon-popup`** — coupons popup.
**`POST /api/ShoppingCart/RegisterLoyaltyCertificate`** — apply LCR.
**`GET /api/user/creditCards`** — saved cards list (see above).
**`GET /api/user/addresses`** — saved addresses (billing + shipping).
**`GET /api/user/emailAddresses`** — saved emails.
**`GET /api/user/phoneNumbers`** — saved phones.
**`GET /api/User/AccountStatus`** — account posture / eligibility.
**`GET /api/User/PublicKeyNonPCI`** — RSA public key for non-PCI field encryption.

### Redux store on the checkout page

The SPA keeps a Redux store accessible via any child fiber:
```js
const dep = document.getElementById('DepartDate');  // or any component
const fiberKey = Object.keys(dep).find(k => k.startsWith('__reactFiber'));
let cur = dep[fiberKey];
while (cur && !(cur.memoizedProps && cur.memoizedProps.store && cur.memoizedProps.store.getState)) cur = cur.return;
window.__uaStore__ = cur.memoizedProps.store;
// Later:
const state = __uaStore__.getState();
const cp = state.commonpayment;  // all selected payment state
```

`commonpayment` slice top-level keys:
- `autoRenew`, `billingAddress1/2/3`, `city`, `state`, `zipCode`, `country`, `countryCode`
- `phoneNumber`, `email`
- `cardInfo` (always empty when a saved card is selected)
- `savedCard` (the full selected card object with `id`, `key`, `lastFourDigits`, etc.)
- `savedEmailAddress`, `savedBillingAddress`, `savedPhoneNumber` (each with full provider keys)
- `taxIdType`, `taxIdNumber`, `taxIdHolder`, `chaseDifferentCard`
- `spouseMPNumber`, `idType`, `idNumber`

### Rich tax breakdown location

`Data.CartData.DisplayCart.DisplayPrices[0].SubItems[]` — each entry
has `Key` (0-based index), `Description`, `Amount`. Verified 8 items
for a domestic round-trip totaling $61.05:

| # | Description | Amount | Binding |
|---|---|---|---|
| 0 | U.S. Transportation Tax | $13.54 | outbound segment |
| 1 | U.S. Transportation Tax | $16.71 | return segment |
| 2 | U.S. Passenger Facility Charge | $4.50 | origin airport (AUS) |
| 3 | U.S. Flight Segment Tax | $5.30 | outbound |
| 4 | Passenger Civil Aviation Security Service Fee | $5.60 | outbound |
| 5 | U.S. Passenger Facility Charge | $4.50 | origin airport (SFO) |
| 6 | U.S. Flight Segment Tax | $5.30 | return |
| 7 | Passenger Civil Aviation Security Service Fee | $5.60 | return |

### Short CartRefId

The human-friendly cart ID shown on the page ("Cart ID: 641457887")
lives at `Data.CartData.CartRefId`. NOT the same as the long UUID
`DisplayCart.CartId` — both are returned in every LoadReservationAndCart
response. Surface both; users remember the short one.

### Aircraft / airport enrichment

Every DisplayTrips[].Flights[] entry has:
- `EquipmentDisclosures.EquipmentType` (IATA 3-char: "738", "7M8")
- `EquipmentDisclosures.EquipmentDescription` ("Boeing 737-800")
- `OriginDescription` ("Austin, TX, US (AUS)")
- `OriginCountryCode`, `OriginStateCode`, `OrgTimezoneOffset`
- Same for `Destination*` fields

Skill now parses these into proper `aircraft` (with `manufacturer:
organization`) and `airport` (with city, countryCode, region) nodes.
Manufacturer lookup table is IATA equipment code → organization node;
only populated for codes actually observed to avoid fabricating data.

### Captured files for future reference

- `/tmp/checkout-intercept.json` — full Network capture of checkout
  page load + seat page + customize-travel page (89 requests)
- `/tmp/rt-intercept.json` — earlier one-way intercept for comparison
- `/tmp/rt3-intercept.json` — round-trip homepage form drive (partial)

## Booking confirmation gate (new in session 2)

`prepare_booking(cart_id)` and `confirm_booking(blob, confirm_amount,
payment_method_last4, dry_run=True)` implement a two-step commit.
See the docstrings in `united.py` for the full contract.

**HMAC key persistence**: the signing key lives at
`~/.agentos/united-booking-key` (atomic write, 0o600 perms). First
call mints it; subsequent calls reuse. `skill_secret.get/set` is
attempted first but doesn't always persist across invocations, so
the file is the authoritative store.

**Gates enforced by `confirm_booking`**:
1. Blob signature verifies (HMAC-SHA256 with persisted key)
2. Blob not expired (5 min TTL)
3. `confirm_amount` EXACT string match to blob total ("USD 464.36")
4. Live cart total hasn't drifted since prepare_booking
5. `payment_method_last4` matches a card from `/api/user/creditCards`
6. `dry_run` must be explicitly `False`

Current state: **all gates verified working via dry_run tests**.
Real checkout POST body shape is not yet captured, so `dry_run=False`
raises RuntimeError rather than guess.

## Session 3 starting state (pick-up checklist)

### What's shipped and tested
- Round-trip booking flow works end-to-end via SPA drive (homepage form
  → Find flights → outbound select → return select → traveler → customize
  → seatmap → checkout) with cart `76695CE3-015A-46F7-84DD-AFF523E427F3`
  kept alive for ~45min, SearchType=2, GrandTotal $464.36.
- `get_cart(cart_id)` works pure-Python (with the correct query params)
- `select_flight` supports round-trip Referer + trip_index
- `search_flights` supports CalendarPricing prime + type-3 return-slice body
- `register_traveler` works on round-trip carts
- `get_seatmap` + `render_seatmap` work on both legs
- `prepare_booking` returns rich `booking_offer` node with itinerary,
  fares, tax_lines, payment_method, signed blob — **depends on a live
  authenticated session**
- `confirm_booking` gates all verified via dry_run

### What's still open
1. **United login is no longer active** — first task of session 3 is
   to re-authenticate. Either: (a) Joe logs in on Brave manually, then
   `store_session_cookies` refreshes credentials, or (b) write a
   proper `login(cdp_port=9222)` tool that opens a CDP-driven sign-in
   flow. The skill has stubs that mention this but the tool isn't
   actually implemented yet.
2. **Capture the final checkout POST body shape.** The endpoint is
   suspected to be `/api/ShoppingCart/checkout` or similar. Options:
   - (A) Drive Agree-and-Purchase for real with intercept — books
     the flight ($464.36) and learns the shape; or
   - (B) CDP `Fetch.enable` scoped to checkout URLs + abort after
     capture — learns shape without booking. Script stub written at
     `/tmp/ua_pause_checkout.py` last session.
3. **Wire the checkout POST into `confirm_booking` dry_run=False path**
   once body shape is known.
4. **Seat selection**: Joe wants to be able to pick an aisle seat.
   `/book-flight/seatmap/<cartId>` URL and `register_seats` tool
   already exist. Need a skill tool to go back to seatmap from
   checkout page (just nav + click logic).
5. **`clear_cart`** — not yet captured. Click "Start Over" in UI with
   intercept running.
6. **`search_flights` pure-Python round-trip mystery**: even with
   CalendarPricing prime + `sc=7,7` Referer + type-3 body, carts
   born via pure HTTP end up SearchType=1. Must CDP-drive the
   homepage form once to mint a SearchType=2 cart. Unsolved.
7. **Skill performance**: every tool call mints a fresh bearer via
   `/api/auth/anonymous-token` and re-fetches profile. Cache both
   per-invocation (Joe flagged the 4s latency as annoying).

