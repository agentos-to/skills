"""
itineraries.py — Render travel itineraries from graph `reservation` nodes.

Provider-agnostic: reads the shared `reservation` shape (see
docs/shapes/reservation.yaml). United, Delta, Amtrak, Airbnb, OpenTable —
all emit the same shape; this renderer reads them all.

Tools:
  list(when="upcoming"|"past"|"all")         → summary rows for a picker
  render(id, format="pdf"|"markdown", ...)   → writes a file, returns path
  render_markdown(reservation)               → pure helper, returns string

Design constraints (see _roadmap/p2/itineraries-skill.md):
  - Shape-contract only: no `_raw.<provider>` reach-in.
  - Pure-Python deps (fpdf2, qrcode+Pillow). Zero system libraries.
  - Helvetica core fonts. Latin-1 only — no Unicode `✈`.
  - US Letter portrait. One page per trip in `reservation.trips[]`.
  - Phase 1 uses a neutral graphite accent (#2B2F36). Phase 2 reads
    `reservation.at.primaryColor` from airline+brand mixin. Phase 3
    introduces `brand_lookup` capability for discoverable brand data.
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

from agentos import returns, sql, timeout


# ── Constants / palette ──────────────────────────────────────────────────────

GRAPH_DB = "~/.agentos/data/agentos.db"

# Phase-1 neutral accent. Phase 2 overrides this per-reservation by reading
# `reservation.at.primaryColor` (provider skills stamp primaryColor on their
# airline node; airline inherits from the brand mixin).
FALLBACK_ACCENT = "#2B2F36"   # graphite
FALLBACK_ON_ACCENT = "#FFFFFF"

INK = (17, 20, 24)            # #111418 — primary ink
MUTE = (107, 114, 128)        # #6B7280 — secondary text
HAIRLINE = (229, 231, 235)    # #E5E7EB — section rules
PAPER = (255, 255, 255)

# Page metrics (mm)
PAGE_W = 216.0   # US Letter
PAGE_H = 279.0
MARGIN_X = 16.0
MARGIN_TOP = 14.0
MARGIN_BOTTOM = 18.0
CONTENT_W = PAGE_W - (2 * MARGIN_X)   # 184 mm

# IATA→month names we rely on (avoid locale drift)
_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# Latin-1 transliteration table. fpdf2 core fonts (Helvetica, Courier) only
# encode Latin-1, so characters we love typographically (em-dash, en-dash,
# smart quotes, `→`, `·`) must be down-mapped for PDF rendering. Markdown
# output keeps the originals — this table only applies to PDF-bound text.
_LATIN1_REPLACEMENTS = {
    "—": "-",       # em-dash —  →  hyphen-minus (close enough at display size)
    "–": "-",       # en-dash –
    "−": "-",       # minus sign −
    "→": ">",       # rightwards arrow →
    "←": "<",       # leftwards arrow ←
    "↔": "<->",     # left-right arrow ↔
    "─": "-",       # box drawings horizontal ─
    "✈": ">",       # airplane ✈ (we draw our own glyph anyway)
    "…": "...",     # horizontal ellipsis …
    "•": "-",       # bullet •  (keep as dash; Latin-1 has \xb7 middle-dot but readers expect a bullet)
    "‘": "'",       # left single quote ‘
    "’": "'",       # right single quote ’
    "“": '"',       # left double quote “
    "”": '"',       # right double quote ”
    "≥": ">=",      # greater-than-or-equal ≥
    "≤": "<=",      # less-than-or-equal ≤
}


def _latin1_safe(s: str) -> str:
    """Down-map non-Latin-1 codepoints for the fpdf2 core fonts.

    Any codepoint not in the replacement table AND not encodable as
    Latin-1 gets NFKD-decomposed and ASCII'd as a last resort, so we
    never raise at render time for an exotic character in a passenger
    name, airport, or URL."""
    if not isinstance(s, str):
        return s
    out = []
    for ch in s:
        repl = _LATIN1_REPLACEMENTS.get(ch)
        if repl is not None:
            out.append(repl)
            continue
        try:
            ch.encode("latin-1")
            out.append(ch)
        except UnicodeEncodeError:
            # NFKD strips the accent off "é" → "e"; drops characters with
            # no ASCII analogue (emoji, CJK). Both are acceptable last-
            # resort behaviors for a core-font PDF.
            import unicodedata
            nfkd = unicodedata.normalize("NFKD", ch)
            out.append(nfkd.encode("ascii", "ignore").decode("ascii"))
    return "".join(out)


# ── Graph read helpers ───────────────────────────────────────────────────────

async def _load_reservations() -> list[dict]:
    """Read every reservation node from the graph.

    Two storage layouts coexist today:

    1. Shape-complete blob — the `value` column holds a JSON array whose
       first element is the full reservation dict (trips, passengers,
       airline, check-in URL). United's post-booking `list_trips` writes
       this layout.
    2. Piecemeal — each field is a separate row in `node_vals` (pre-v3
       cart holds, partial data). We reassemble from flat keys.

    Returns a list of reservation dicts. Each dict is annotated with
    `_node_id` so the renderer can look itself up later if needed.
    """
    # One query pulls both layouts: grab all value rows for nodes tagged
    # with the `reservation` shape, then fold piecemeal rows into a single
    # dict per node_id.
    rows = await sql.query(
        """
        SELECT nv.node_id, nv.key, nv.value
        FROM node_vals nv
        JOIN node_shapes ns ON ns.node_id = nv.node_id
        JOIN nodes n ON n.id = nv.node_id
        WHERE ns.shape_name = 'reservation'
          AND n.deleted_at IS NULL
        """,
        db=GRAPH_DB,
    )

    by_node: dict[str, dict] = {}
    for row in rows:
        node_id, key, value = row["node_id"], row["key"], row["value"]
        bucket = by_node.setdefault(node_id, {})
        bucket[key] = value

    reservations: list[dict] = []
    for node_id, fields in by_node.items():
        if "value" in fields:
            # Shape-complete layout. JSON-parse and unwrap the single-item
            # array the engine wraps around structured shape data.
            try:
                payload = json.loads(fields["value"])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, list) and payload:
                rec = payload[0]
            elif isinstance(payload, dict):
                rec = payload
            else:
                continue
            if isinstance(rec, dict):
                rec["_node_id"] = node_id
                reservations.append(rec)
        else:
            # Piecemeal — fold flat key/value rows into a shallow dict.
            rec = {k: v for k, v in fields.items()}
            rec["_node_id"] = node_id
            rec["_partial"] = True
            reservations.append(rec)

    return reservations


def _match_id(rec: dict, target: str) -> bool:
    """True if the reservation record matches the caller-supplied id.

    Accepts: business id (`united-pnr:OSKNPT`), bare PNR (`OSKNPT`),
    or node id (`wwkx39`).
    """
    if not target:
        return False
    target_l = target.lower()
    candidates = [
        rec.get("id"),
        rec.get("reservationId"),
        rec.get("_node_id"),
    ]
    for c in candidates:
        if c and str(c).lower() == target_l:
            return True
    # Allow "united-pnr:OSKNPT" → match against id that ends with target
    rid = rec.get("id") or ""
    if rid and rid.lower().endswith(":" + target_l):
        return True
    return False


# ── Formatting helpers ───────────────────────────────────────────────────────

def _parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO-8601 string into a naive datetime. Returns None on miss."""
    if not s or not isinstance(s, str):
        return None
    try:
        # `datetime.fromisoformat` handles both `2026-04-28T13:00:00` and
        # offset-qualified forms. Strip trailing `Z` which older pythons don't
        # accept (we have 3.14 but be portable).
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _fmt_date_long(dt: datetime | None) -> str:
    """Mon · Apr 28 2026  — hero-band DEPARTS/ARRIVES subtitle."""
    if not dt:
        return "—"
    return f"{_WEEKDAYS[dt.weekday()]} · {_MONTHS[dt.month]} {dt.day} {dt.year}"


def _fmt_date_short(dt: datetime | None) -> str:
    """Tue, Apr 28 2026 — compact row form."""
    if not dt:
        return "—"
    return f"{_WEEKDAYS[dt.weekday()]}, {_MONTHS[dt.month]} {dt.day} {dt.year}"


def _fmt_time(dt: datetime | None) -> str:
    """12-hour clock, no leading zero. `1:00 PM`."""
    if not dt:
        return "—"
    h = dt.hour % 12 or 12
    m = dt.minute
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{h}:{m:02d} {ampm}"


def _duration(dep: datetime | None, arr: datetime | None,
              origin_iata: str | None, dest_iata: str | None) -> str:
    """`4h 02m`. Timezone-aware when both airports are in our tz table;
    returns '—' otherwise (rather than the wrong number from naive clock
    arithmetic).

    The shape carries local times (`1:00 PM` AUS, `3:02 PM` SFO). Naive
    subtraction says 2h 02m — wrong. Real flight is 4h 02m. We resolve
    via a small static iata→tz table covering the major US airports; for
    long-tail airports we'd rather show '—' than a lie.
    """
    if not dep or not arr or not origin_iata or not dest_iata:
        return "-"
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return "-"
    o_tz = _IATA_TZ.get(origin_iata.upper())
    d_tz = _IATA_TZ.get(dest_iata.upper())
    if not o_tz or not d_tz:
        return "-"
    dep_utc = dep.replace(tzinfo=ZoneInfo(o_tz))
    arr_utc = arr.replace(tzinfo=ZoneInfo(d_tz))
    secs = int((arr_utc - dep_utc).total_seconds())
    if secs <= 0:
        return "-"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m"


# IATA → IANA timezone. Limited to the airports the graph currently sees;
# extended as new airports show up on new reservations. Full coverage (in
# the thousands of airports) belongs in the graph itself as an `airport.tz`
# field — add it when the first long-tail airport shows up on a booking.
_IATA_TZ: dict[str, str] = {
    # US — grouped by tz
    "JFK": "America/New_York", "LGA": "America/New_York", "EWR": "America/New_York",
    "BOS": "America/New_York", "IAD": "America/New_York", "DCA": "America/New_York",
    "BWI": "America/New_York", "PHL": "America/New_York", "ATL": "America/New_York",
    "MIA": "America/New_York", "FLL": "America/New_York", "MCO": "America/New_York",
    "TPA": "America/New_York", "CLT": "America/New_York", "RDU": "America/New_York",
    "DTW": "America/New_York", "CLE": "America/New_York", "PIT": "America/New_York",
    "ORD": "America/Chicago", "MDW": "America/Chicago", "MSP": "America/Chicago",
    "DFW": "America/Chicago", "DAL": "America/Chicago", "IAH": "America/Chicago",
    "HOU": "America/Chicago", "AUS": "America/Chicago", "SAT": "America/Chicago",
    "STL": "America/Chicago", "MCI": "America/Chicago", "MSY": "America/Chicago",
    "MEM": "America/Chicago", "BNA": "America/Chicago", "OKC": "America/Chicago",
    "DEN": "America/Denver", "SLC": "America/Denver", "ABQ": "America/Denver",
    "BOI": "America/Boise",
    "PHX": "America/Phoenix",  # no DST
    "LAX": "America/Los_Angeles", "SFO": "America/Los_Angeles", "SJC": "America/Los_Angeles",
    "OAK": "America/Los_Angeles", "SAN": "America/Los_Angeles", "LAS": "America/Los_Angeles",
    "SEA": "America/Los_Angeles", "PDX": "America/Los_Angeles", "SMF": "America/Los_Angeles",
    "ANC": "America/Anchorage", "FAI": "America/Anchorage",
    "HNL": "Pacific/Honolulu", "OGG": "Pacific/Honolulu", "KOA": "Pacific/Honolulu",
    # Canada
    "YYZ": "America/Toronto", "YUL": "America/Toronto", "YOW": "America/Toronto",
    "YYC": "America/Edmonton", "YEG": "America/Edmonton",
    "YVR": "America/Vancouver",
    # Mexico
    "MEX": "America/Mexico_City", "GDL": "America/Mexico_City",
    "CUN": "America/Cancun",
    # Europe
    "LHR": "Europe/London", "LGW": "Europe/London", "STN": "Europe/London",
    "CDG": "Europe/Paris", "ORY": "Europe/Paris",
    "FRA": "Europe/Berlin", "MUC": "Europe/Berlin", "BER": "Europe/Berlin",
    "AMS": "Europe/Amsterdam",
    "MAD": "Europe/Madrid", "BCN": "Europe/Madrid",
    "FCO": "Europe/Rome", "MXP": "Europe/Rome",
    "ZRH": "Europe/Zurich",
    # Asia
    "NRT": "Asia/Tokyo", "HND": "Asia/Tokyo",
    "ICN": "Asia/Seoul",
    "HKG": "Asia/Hong_Kong",
    "SIN": "Asia/Singapore",
    "BKK": "Asia/Bangkok",
    "DXB": "Asia/Dubai",
    "DEL": "Asia/Kolkata", "BOM": "Asia/Kolkata",
    # Oceania
    "SYD": "Australia/Sydney", "MEL": "Australia/Sydney",
    "AKL": "Pacific/Auckland",
    # Africa / S. America — extend as needed
    "GRU": "America/Sao_Paulo",
    "EZE": "America/Argentina/Buenos_Aires",
    "JNB": "Africa/Johannesburg",
    "CPT": "Africa/Johannesburg",
}


def _legalname(passenger: dict) -> str:
    """`GIUSEPPEEFISIO CONTINI` → `Giuseppe Efisio Contini`.

    The united shape stores passenger names as passport MRZ (uppercase, no
    casing). We title-case for display. Non-western names are untouched —
    title-casing CJK is a no-op.
    """
    name = (passenger.get("legalName")
            or passenger.get("name")
            or " ".join(filter(None, [
                passenger.get("givenName"), passenger.get("familyName")
            ]))
            or "")
    if not name:
        return ""
    # If the name contains any lowercase, trust the caller; otherwise,
    # title-case word by word.
    if any(c.islower() for c in name):
        return name
    # MRZ often runs given names together (GIUSEPPEEFISIO). We don't try
    # to un-concatenate — just title-case what we have. A future pass can
    # split on known given-name boundaries.
    return " ".join(w.capitalize() for w in name.split())


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    """`#002244` or `002244` → (0, 34, 68). Returns the neutral accent if
    the input doesn't look like a 6-char hex."""
    if not h:
        return _hex_to_rgb(FALLBACK_ACCENT)
    s = h.lstrip("#")
    if len(s) != 6 or not re.fullmatch(r"[0-9A-Fa-f]{6}", s):
        return _hex_to_rgb(FALLBACK_ACCENT)
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _tint(rgb: tuple[int, int, int], alpha: float = 0.10) -> tuple[int, int, int]:
    """Approximate (1 - alpha) * white + alpha * rgb. Used for the hero
    band background — a very faint wash of the brand color."""
    r, g, b = rgb
    mix = lambda c: int(round(255 * (1 - alpha) + c * alpha))  # noqa: E731
    return (mix(r), mix(g), mix(b))


def _accent_rgb(reservation: dict) -> tuple[int, int, int]:
    """The brand accent for this reservation's header/hero band.

    Reads `reservation.at.primaryColor` (set by provider skills that carry
    the airline+brand mixin). Falls back to neutral graphite if missing —
    Phase 1 renders look intentional, not broken, before Phase 2 brand
    data lands.
    """
    at = reservation.get("at") or {}
    primary = at.get("primaryColor") if isinstance(at, dict) else None
    return _hex_to_rgb(primary or FALLBACK_ACCENT)


def _on_accent_rgb(reservation: dict) -> tuple[int, int, int]:
    """Text color over the accent band. Reads `textColor`; falls back to
    white."""
    at = reservation.get("at") or {}
    tc = at.get("textColor") if isinstance(at, dict) else None
    return _hex_to_rgb(tc or FALLBACK_ON_ACCENT)


# ── Trip-data extraction ─────────────────────────────────────────────────────

def _trips(reservation: dict) -> list[dict]:
    """Always return a list — handle the `trips` missing case so callers
    don't sprinkle `if trips`."""
    trips = reservation.get("trips") or []
    if isinstance(trips, dict):
        trips = [trips]
    return [t for t in trips if isinstance(t, dict)]


def _first_leg(trip: dict) -> dict:
    """A trip with zero legs renders empty — still valid, still a page."""
    legs = trip.get("legs") or []
    return legs[0] if legs and isinstance(legs[0], dict) else {}


def _primary_city(reservation: dict) -> str:
    """The city the trip goes *to*. For a round-trip (AUS→SFO, SFO→AUS)
    that's the destination of the first trip, not the last. Used to
    auto-name the `~/Documents/Travel/YYYY-MM <city>/` folder."""
    trips = _trips(reservation)
    if not trips:
        return ""
    dest = trips[0].get("destination") or {}
    return dest.get("city") or ""


def _start_dt(reservation: dict) -> datetime | None:
    return _parse_iso(reservation.get("startTime"))


def _end_dt(reservation: dict) -> datetime | None:
    return _parse_iso(reservation.get("endTime"))


def _pnr(reservation: dict) -> str:
    return (reservation.get("reservationId")
            or str(reservation.get("id") or "").split(":")[-1]
            or "")


def _provider_name(reservation: dict) -> str:
    at = reservation.get("at") or {}
    return (at.get("name") if isinstance(at, dict) else None) or ""


def _provider_slug(reservation: dict) -> str:
    """`united`, `delta`, … — used in the output filename. Prefers IATA
    code when present."""
    at = reservation.get("at") or {}
    if isinstance(at, dict):
        iata = at.get("iataCode")
        if iata:
            return iata.strip().upper()
        name = at.get("name") or ""
        return "".join(c for c in name if c.isalnum()) or "provider"
    return "provider"


# ── PDF render ───────────────────────────────────────────────────────────────

class ItineraryPDF(FPDF):
    """FPDF subclass with a header/footer that fires on every add_page().

    The header carries the trip title + PNR; the footer carries the
    booking-time / total / page counter. Per-trip body is rendered by
    _render_trip_page() after the add_page() call.
    """

    def __init__(self, reservation: dict):
        super().__init__(orientation="P", unit="mm", format="letter")
        self.reservation = reservation
        self.set_margins(MARGIN_X, MARGIN_TOP, MARGIN_X)
        self.set_auto_page_break(True, margin=MARGIN_BOTTOM)
        self.accent = _accent_rgb(reservation)
        self.on_accent = _on_accent_rgb(reservation)
        self.tint = _tint(self.accent)
        self.set_title(_latin1_safe(f"Itinerary {_pnr(reservation)}"))
        self.set_author(_latin1_safe(_provider_name(reservation)))
        self.set_creator("agentOS - itineraries skill")

    # fpdf2 core fonts are Latin-1 only. Route every cell write through
    # the transliteration map so we don't crash on an em-dash or arrow
    # somewhere in the graph data. (FPDF has `normalize_text` as an
    # extension hook but it runs too late — by the time it's called the
    # text has already been passed through encode/decode and blown up.)
    def cell(self, w=0, h=0, text="", *args, **kwargs):
        return super().cell(w, h, _latin1_safe(str(text)), *args, **kwargs)

    def multi_cell(self, w, h=0, text="", *args, **kwargs):
        return super().multi_cell(w, h, _latin1_safe(str(text)), *args, **kwargs)

    # Header prints on every add_page(). Trip title left, PNR right.
    def header(self):
        trips = _trips(self.reservation)
        idx = self.page_no() - 1
        trip = trips[idx] if idx < len(trips) else {}
        dest = (trip.get("destination") or {}).get("city") or _primary_city(self.reservation)
        title = f"Trip to {dest}" if dest else "Trip"

        self.set_xy(MARGIN_X, MARGIN_TOP - 4)
        self.set_text_color(*INK)
        self.set_font("helvetica", "B", 14)
        self.cell(CONTENT_W * 0.6, 7, title, new_x="LMARGIN", new_y="TOP")

        # PNR right-aligned, monospace.
        pnr = _pnr(self.reservation)
        if pnr:
            self.set_xy(MARGIN_X + CONTENT_W * 0.6, MARGIN_TOP - 4)
            self.set_font("courier", "B", 14)
            self.set_text_color(*self.accent)
            self.cell(CONTENT_W * 0.4, 7, pnr, new_x="LMARGIN", new_y="TOP", align="R")

        # Hairline under the header.
        self.set_draw_color(*HAIRLINE)
        self.set_line_width(0.2)
        y = MARGIN_TOP + 6
        self.line(MARGIN_X, y, MARGIN_X + CONTENT_W, y)

        # Reset cursor below the rule so the body starts on a known line.
        self.set_xy(MARGIN_X, y + 6)

    # Footer: booking time · total · page counter. Muted.
    def footer(self):
        r = self.reservation
        booked = _parse_iso(r.get("bookingTime"))
        parts: list[str] = []
        if booked:
            parts.append(f"Booked {_MONTHS[booked.month]} {booked.day} {booked.year}")
        total = r.get("totalAmount")
        ccy = r.get("currency") or "USD"
        if total is not None:
            try:
                parts.append(f"Total {float(total):.2f} {ccy}")
            except (TypeError, ValueError):
                pass
        page_no = self.page_no()
        total_pages = max(1, len(_trips(r)))
        parts.append(f"Page {page_no} of {total_pages}")

        self.set_y(-(MARGIN_BOTTOM - 4))
        self.set_font("helvetica", "", 7.5)
        self.set_text_color(*MUTE)
        self.cell(CONTENT_W, 5, " · ".join(parts), align="C")

    # ── body ────────────────────────────────────────────────────────────────

    def render_all(self):
        trips = _trips(self.reservation)
        if not trips:
            # Minimal single-page form for a reservation with no trips
            # (holds, dining, lodging before trip data lands).
            self.add_page()
            self._render_no_trip_page()
            return
        for trip in trips:
            self.add_page()
            self._render_trip_page(trip)

    def _render_no_trip_page(self):
        """Blank reservation — just show what we know. Better than error."""
        self.set_xy(MARGIN_X, MARGIN_TOP + 20)
        self.set_font("helvetica", "", 11)
        self.set_text_color(*MUTE)
        name = self.reservation.get("name") or self.reservation.get("id") or "Reservation"
        self.cell(CONTENT_W, 7, str(name))

    def _render_trip_page(self, trip: dict):
        """One page: DEPARTS label → hero band → flight info → passenger
        → check-in. Each section elides gracefully if data is missing."""
        leg = _first_leg(trip)

        # Section 1: DEPARTS <DATE> ---------------------------------------
        dep_dt = _parse_iso(leg.get("departureTime") or trip.get("departureTime"))
        self._section_label("DEPARTS " + _fmt_date_long(dep_dt).upper())

        # Section 2: hero band --------------------------------------------
        self._render_hero_band(trip, leg)

        # Section 3: flight details ---------------------------------------
        self._section_rule("FLIGHT DETAILS")
        self._render_flight_details_grid(trip, leg)

        # Section 4: passenger(s) -----------------------------------------
        self._section_rule("PASSENGER" + ("S" if len(self.reservation.get("passengers") or []) > 1 else ""))
        self._render_passengers()

        # Section 5: check-in ---------------------------------------------
        checkin_url = self.reservation.get("checkinUrl")
        if checkin_url:
            self._section_rule("CHECK-IN")
            self._render_checkin(str(checkin_url))

    # ── sections ────────────────────────────────────────────────────────────

    def _section_label(self, text: str):
        """Tiny uppercase label. Used before hero band and inside grids."""
        self.set_font("helvetica", "B", 8)
        self.set_text_color(*MUTE)
        self.cell(CONTENT_W, 5, text)
        self.ln(7)

    def _section_rule(self, label: str):
        """Uppercase section label inside a hairline rule that runs across
        the content box — visual anchor for scanning the page."""
        self.ln(3)
        y = self.get_y()
        self.set_draw_color(*HAIRLINE)
        self.set_line_width(0.2)
        self.line(MARGIN_X, y, MARGIN_X + CONTENT_W, y)
        self.set_xy(MARGIN_X, y + 2)
        self.set_font("helvetica", "B", 8)
        self.set_text_color(*self.accent)
        self.cell(CONTENT_W, 5, label)
        self.ln(8)

    def _render_hero_band(self, trip: dict, leg: dict):
        """The visual anchor of the page: two big 3-letter airport codes
        with a dashed route + plane glyph between them, on a 10%-tint
        brand wash."""
        # Band metrics
        band_h = 56.0
        band_y = self.get_y()
        band_x = MARGIN_X

        # Tint fill
        self.set_fill_color(*self.tint)
        self.rect(band_x, band_y, CONTENT_W, band_h, "F")

        # Extract codes + cities
        origin = trip.get("origin") or (leg.get("departsFrom") or {})
        dest = trip.get("destination") or (leg.get("arrivesAt") or {})
        o_code = (origin.get("iataCode") or "—").strip().upper()
        d_code = (dest.get("iataCode") or "—").strip().upper()
        o_city = origin.get("city") or ""
        d_city = dest.get("city") or ""
        o_region = origin.get("region") or ""
        d_region = dest.get("region") or ""
        o_country = origin.get("countryCode") or ""
        d_country = dest.get("countryCode") or ""

        dep_dt = _parse_iso(leg.get("departureTime") or trip.get("departureTime"))
        arr_dt = _parse_iso(leg.get("arrivalTime") or trip.get("arrivalTime"))

        # Left column: origin
        inner_pad = 8.0
        col_w = 50.0
        col_y = band_y + 10
        self.set_text_color(*INK)

        # 72pt airport code — hero typography. FPDF sets font size in pt.
        self.set_font("helvetica", "B", 72)
        self.set_xy(band_x + inner_pad, col_y)
        self.cell(col_w, 25, o_code)

        # Time beneath (28pt bold)
        self.set_font("helvetica", "B", 14)
        self.set_text_color(*INK)
        self.set_xy(band_x + inner_pad, col_y + 27)
        self.cell(col_w, 6, _fmt_time(dep_dt))

        # City (11pt regular, uppercase — mimic the spec's tracking hint
        # by just using uppercase; fpdf2 core fonts have no tracking API)
        self.set_font("helvetica", "", 9.5)
        self.set_text_color(*MUTE)
        self.set_xy(band_x + inner_pad, col_y + 34)
        self.cell(col_w, 5, _trim_city(o_city, o_region, o_country))

        # Right column: destination
        right_col_x = band_x + CONTENT_W - col_w - inner_pad
        self.set_font("helvetica", "B", 72)
        self.set_text_color(*INK)
        self.set_xy(right_col_x, col_y)
        self.cell(col_w, 25, d_code, align="L")

        self.set_font("helvetica", "B", 14)
        self.set_xy(right_col_x, col_y + 27)
        self.cell(col_w, 6, _fmt_time(arr_dt))

        self.set_font("helvetica", "", 9.5)
        self.set_text_color(*MUTE)
        self.set_xy(right_col_x, col_y + 34)
        self.cell(col_w, 5, _trim_city(d_city, d_region, d_country))

        # Dashed route line + plane glyph between the two codes
        route_y = col_y + 12
        route_x1 = band_x + inner_pad + col_w + 4
        route_x2 = right_col_x - 4
        if route_x2 > route_x1:
            self.set_draw_color(*self.accent)
            self.set_line_width(0.4)
            self.set_dash_pattern(dash=1.2, gap=1.8)
            self.line(route_x1, route_y, route_x2, route_y)
            self.set_dash_pattern()  # reset to solid

            # Plane glyph — a small filled polygon centered on the line.
            # Latin-1 only, so no unicode `✈`. 8-vertex polygon mimics a
            # simple plane silhouette pointing right.
            cx = (route_x1 + route_x2) / 2.0
            self._plane_glyph(cx, route_y)

        # Drop the cursor past the band
        self.set_y(band_y + band_h + 4)

    def _plane_glyph(self, cx: float, cy: float):
        """Draw a simple right-pointing plane, ~6mm wide, at (cx, cy).

        fpdf2 doesn't have a primitive for plane; we compose a filled
        polygon from the rough silhouette: fuselage nose, main wings,
        tail fin.
        """
        a = 3.0   # half-length fuselage (long axis)
        w = 0.6   # fuselage thickness
        wing_s = 1.6   # wing span (half)
        wing_c = 0.3   # wing chord
        tail_s = 1.0   # tail fin span (half)
        tail_c = 0.35  # tail fin chord

        # Vertices traced clockwise from nose:
        pts = [
            (cx + a,        cy),             # nose
            (cx + a * 0.2,  cy + w),         # upper-right fuselage
            (cx - a * 0.2,  cy + wing_s),    # right wing tip front-edge
            (cx - a * 0.2 - wing_c, cy + wing_s),  # right wing tip rear-edge
            (cx - a * 0.4,  cy + w),         # upper-mid fuselage
            (cx - a * 0.75, cy + tail_s),    # right tail tip front-edge
            (cx - a * 0.75 - tail_c, cy + tail_s),  # right tail tip rear-edge
            (cx - a,        cy + w),         # tail
            (cx - a,        cy - w),         # tail (mirrored)
            (cx - a * 0.75 - tail_c, cy - tail_s),
            (cx - a * 0.75, cy - tail_s),
            (cx - a * 0.4,  cy - w),
            (cx - a * 0.2 - wing_c, cy - wing_s),
            (cx - a * 0.2,  cy - wing_s),
            (cx + a * 0.2,  cy - w),
        ]
        self.set_fill_color(*self.accent)
        # fpdf2's polygon() accepts a list of (x,y) tuples.
        self.polygon(pts, style="F")

    def _render_flight_details_grid(self, trip: dict, leg: dict):
        """Key/value pairs in two columns. Missing values render as '—'.

        Fields: Flight, Cabin, Duration, Aircraft. Aircraft and cabin are
        often None in MyTrips (see spec "fields we'd like but don't have").
        """
        dep_dt = _parse_iso(leg.get("departureTime") or trip.get("departureTime"))
        arr_dt = _parse_iso(leg.get("arrivalTime") or trip.get("arrivalTime"))

        origin_iata = (trip.get("origin") or leg.get("departsFrom") or {}).get("iataCode")
        dest_iata = (trip.get("destination") or leg.get("arrivesAt") or {}).get("iataCode")
        duration = _duration(dep_dt, arr_dt, origin_iata, dest_iata)

        flight_no = leg.get("flightNumber") or "—"
        aircraft = _aircraft_label(leg)
        cabin = _cabin_label(leg)

        self._kv_row([
            ("Flight", flight_no),
            ("Cabin", cabin),
        ])
        self._kv_row([
            ("Duration", duration),
            ("Aircraft", aircraft),
        ])

    def _kv_row(self, pairs: list[tuple[str, str]]):
        """Two-column key/value row. Each pair consumes half the content
        width; the label is muted/small and the value is body-size."""
        col_w = CONTENT_W / 2.0
        label_w = 28.0
        y = self.get_y()
        for i, (label, value) in enumerate(pairs):
            x = MARGIN_X + (col_w * i)
            self.set_xy(x, y)
            self.set_font("helvetica", "", 9)
            self.set_text_color(*MUTE)
            self.cell(label_w, 5, label)
            self.set_xy(x + label_w, y)
            self.set_font("helvetica", "", 10)
            self.set_text_color(*INK)
            self.cell(col_w - label_w, 5, str(value))
        self.ln(6)

    def _render_passengers(self):
        passengers = self.reservation.get("passengers") or []
        if not passengers:
            self.set_font("helvetica", "", 10)
            self.set_text_color(*MUTE)
            self.cell(CONTENT_W, 5, "—")
            self.ln(6)
            return
        for p in passengers:
            name = _legalname(p)
            # Collect any loyalty memberships — e.g. MileagePlus XX118941
            membership_strs: list[str] = []
            for m in (p.get("memberships") or []):
                at = m.get("at") or {}
                label_parts = []
                program = _program_label(at, m)
                if program:
                    label_parts.append(program)
                mid = m.get("id") or m.get("identifier")
                if mid:
                    label_parts.append(str(mid))
                if label_parts:
                    membership_strs.append(" ".join(label_parts))
            right = "   ·   ".join(membership_strs)

            y = self.get_y()
            self.set_xy(MARGIN_X, y)
            self.set_font("helvetica", "B", 11)
            self.set_text_color(*INK)
            self.cell(CONTENT_W * 0.55, 5, name)
            if right:
                self.set_xy(MARGIN_X + CONTENT_W * 0.55, y)
                self.set_font("helvetica", "", 10)
                self.set_text_color(*MUTE)
                self.cell(CONTENT_W * 0.45, 5, right, align="R")
            self.ln(6)

    def _render_checkin(self, url: str):
        """QR + URL. If qrcode isn't importable we elide the QR but still
        show the URL — skill stays useful on a minimal install."""
        y = self.get_y()
        qr_sz = 22.0
        qr_x = MARGIN_X
        try:
            qr_png = _qr_png(url)
        except Exception:
            qr_png = None

        if qr_png is not None:
            # Border in the accent color
            self.set_draw_color(*self.accent)
            self.set_line_width(0.4)
            self.rect(qr_x, y, qr_sz, qr_sz)
            self.image(qr_png, x=qr_x + 1.5, y=y + 1.5, w=qr_sz - 3, h=qr_sz - 3)

        text_x = qr_x + qr_sz + 6
        self.set_xy(text_x, y + 2)
        self.set_font("helvetica", "", 10)
        self.set_text_color(*INK)
        self.cell(CONTENT_W - (text_x - MARGIN_X), 5, "Opens 24h before departure")
        self.set_xy(text_x, y + 9)
        self.set_font("courier", "", 9)
        self.set_text_color(*MUTE)
        self.cell(CONTENT_W - (text_x - MARGIN_X), 5, url)
        self.ln(qr_sz + 3)


# ── Per-leg label helpers ────────────────────────────────────────────────────

def _trim_city(city: str, region: str, country: str) -> str:
    """`Austin, TX, US`. Fold empty parts."""
    parts = [p for p in (city, region, country) if p]
    return ", ".join(parts) or "—"


def _cabin_label(leg: dict) -> str:
    """Extract a cabin label from whatever the leg carries.

    We read shape-canonical fields first (`cabin`, `fareClass`). If only
    a single-letter booking class is present we map the common ones; else
    we render '—'. No `_raw` reach-in.
    """
    for key in ("cabin", "cabinName", "fareClass"):
        v = leg.get(key)
        if v:
            return str(v)
    bc = leg.get("_bookingClass")
    if isinstance(bc, str) and bc.strip():
        code = bc.strip().upper()
        # Common United domestic codes. Keep conservative — we'd rather
        # show the letter than lie about it.
        lookup = {
            "N": "Basic Economy (N)",
            "G": "Economy (G)",
            "Y": "Economy (Y)",
            "B": "Economy (B)",
            "M": "Economy (M)",
            "E": "Economy Plus (E)",
            "K": "Economy Saver (K)",
        }
        return lookup.get(code, f"Class {code}")
    return "—"


def _aircraft_label(leg: dict) -> str:
    """Shape field `aircraft` may be an organization node or a string.
    Either way, return a display label (falls back to '—')."""
    a = leg.get("aircraft")
    if not a:
        return "—"
    if isinstance(a, dict):
        return a.get("name") or a.get("model") or "—"
    return str(a) or "—"


def _program_label(at: dict, membership: dict) -> str:
    """`MileagePlus`, `SkyMiles` — the airline-specific program name if we
    know it, else a generic 'Member' label."""
    known = {
        "UA": "MileagePlus",
        "DL": "SkyMiles",
        "AA": "AAdvantage",
        "LH": "Miles & More",
        "AF": "Flying Blue",
        "KL": "Flying Blue",
        "BA": "Executive Club",
        "SQ": "KrisFlyer",
        "NH": "ANA Mileage Club",
        "JL": "JAL Mileage Bank",
    }
    iata = (at.get("iataCode") or "").strip().upper()
    if iata in known:
        return known[iata]
    # Membership might carry its own program name.
    name = membership.get("name") or ""
    if "MileagePlus" in name:
        return "MileagePlus"
    return "Member"


def _qr_png(url: str) -> io.BytesIO:
    """Render a QR code to an in-memory PNG. Imported lazily so the skill
    still loads if qrcode/Pillow aren't available yet; the caller catches
    ImportError and drops the QR."""
    import qrcode  # noqa: WPS433 — lazy import by design (see docstring)
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ── Markdown render ──────────────────────────────────────────────────────────

def render_markdown_from(reservation: dict) -> str:
    """Pure helper (module-level) — no decorators, no I/O. Returns a
    markdown string describing the reservation. Useful for chat/email
    previews or the `format="markdown"` branch of `render`."""
    lines: list[str] = []
    pnr = _pnr(reservation)
    provider = _provider_name(reservation) or "Travel"
    primary_city = _primary_city(reservation)
    title = f"# {provider} · Trip to {primary_city}" if primary_city else f"# {provider} reservation"
    lines.append(title)
    if pnr:
        lines.append(f"**Confirmation:** `{pnr}`")

    start = _start_dt(reservation)
    end = _end_dt(reservation)
    if start or end:
        dates = []
        if start:
            dates.append(_fmt_date_short(start))
        if end:
            dates.append(_fmt_date_short(end))
        lines.append("**Dates:** " + " → ".join(dates))
    lines.append("")

    for i, trip in enumerate(_trips(reservation), start=1):
        leg = _first_leg(trip)
        origin = trip.get("origin") or (leg.get("departsFrom") or {})
        dest = trip.get("destination") or (leg.get("arrivesAt") or {})
        dep = _parse_iso(leg.get("departureTime") or trip.get("departureTime"))
        arr = _parse_iso(leg.get("arrivalTime") or trip.get("arrivalTime"))

        lines.append(f"## Leg {i}: {origin.get('iataCode', '??')} → {dest.get('iataCode', '??')}")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("|---|---|")
        lines.append(f"| Depart | {_fmt_date_short(dep)} · {_fmt_time(dep)} ({origin.get('name', '')}) |")
        lines.append(f"| Arrive | {_fmt_date_short(arr)} · {_fmt_time(arr)} ({dest.get('name', '')}) |")
        lines.append(f"| Flight | {leg.get('flightNumber', '—')} |")
        lines.append(f"| Cabin | {_cabin_label(leg)} |")
        lines.append(f"| Aircraft | {_aircraft_label(leg)} |")
        lines.append(f"| Duration | {_duration(dep, arr, origin.get('iataCode'), dest.get('iataCode'))} |")
        lines.append("")

    passengers = reservation.get("passengers") or []
    if passengers:
        lines.append("## Passengers")
        for p in passengers:
            name = _legalname(p)
            ms = []
            for m in (p.get("memberships") or []):
                at = m.get("at") or {}
                label = _program_label(at, m)
                mid = m.get("id") or m.get("identifier") or ""
                ms.append(f"{label} {mid}".strip())
            suffix = f" — {', '.join(ms)}" if ms else ""
            lines.append(f"- {name}{suffix}")
        lines.append("")

    checkin = reservation.get("checkinUrl")
    if checkin:
        lines.append(f"**Check-in:** <{checkin}>")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ── Output-file helpers ──────────────────────────────────────────────────────

def _pick_out_dir(reservation: dict) -> Path:
    """`~/Documents/Travel/YYYY-MM <primary-city>/` — auto-create. Falls
    back to `~/Documents/Travel/` if no city is known."""
    base = Path.home() / "Documents" / "Travel"
    start = _start_dt(reservation)
    city = _primary_city(reservation)
    if start and city:
        sub = f"{start.year:04d}-{start.month:02d} {city}"
    elif start:
        sub = f"{start.year:04d}-{start.month:02d}"
    else:
        sub = ""
    return (base / sub) if sub else base


def _out_filename(reservation: dict, ext: str) -> str:
    """`2026-04-28 United OSKNPT Itinerary.pdf` — date-first so the folder
    sorts chronologically, and Finder's name-sort groups a trip's files."""
    start = _start_dt(reservation)
    date_str = f"{start.year:04d}-{start.month:02d}-{start.day:02d}" if start else "unknown-date"
    provider = _provider_name(reservation) or _provider_slug(reservation)
    pnr = _pnr(reservation) or ""
    parts = [date_str, provider, pnr, "Itinerary"]
    return " ".join(p for p in parts if p) + "." + ext


# ── Tools ────────────────────────────────────────────────────────────────────

@returns("object[]")
@timeout(30)
async def list(when: str = "upcoming", **_params) -> dict:
    """List reservations on the graph. Filters by `startTime`/`endTime`.

    Args:
      when: `"upcoming"` (endTime ≥ now, default), `"past"` (endTime < now),
            `"all"` (no time filter).
    Returns:
      {"reservations": [...]} — one dict per reservation with a summary
      suitable for an AskUserQuestion picker.
    """
    all_recs = await _load_reservations()
    now = datetime.now()

    def pick(r: dict) -> bool:
        end = _end_dt(r) or _start_dt(r)
        if when == "all" or end is None:
            return True
        if when == "past":
            return end < now
        return end >= now

    rows = []
    for r in sorted(all_recs, key=lambda x: _start_dt(x) or datetime.min):
        if not pick(r):
            continue
        trips = _trips(r)
        cities = " → ".join(
            t.get("destination", {}).get("iataCode") or "?"
            for t in trips
        )
        if trips:
            cities = (trips[0].get("origin") or {}).get("iataCode", "?") + " → " + cities
        start = _start_dt(r)
        end = _end_dt(r)
        rows.append({
            "id": r.get("id") or r.get("_node_id"),
            "node_id": r.get("_node_id"),
            "name": r.get("name"),
            "provider": _provider_name(r),
            "pnr": _pnr(r),
            "startTime": r.get("startTime"),
            "endTime": r.get("endTime"),
            "dates": " → ".join(filter(None, [
                _fmt_date_short(start) if start else None,
                _fmt_date_short(end) if end else None,
            ])),
            "cities": cities,
            "total": r.get("totalAmount"),
            "currency": r.get("currency"),
            "status": r.get("status"),
        })

    return {"reservations": rows}


@returns({"path": "string", "format": "string", "pages": "integer"})
@timeout(60)
async def render(id: str, format: str = "pdf", out_dir: str | None = None, **_params) -> dict:
    """Render a reservation to a file.

    Args:
      id: Reservation id (`united-pnr:OSKNPT`), bare PNR (`OSKNPT`), or
          node id (`wwkx39`). Fuzzy-matched against each candidate field.
      format: `"pdf"` (default) or `"markdown"`.
      out_dir: Override the auto-picked `~/Documents/Travel/<YYYY-MM …>/`.

    Returns:
      {"path": "<absolute path>", "format": "...", "pages": N}
    """
    fmt = (format or "pdf").lower()
    if fmt not in ("pdf", "markdown", "md"):
        return {"error": f"unknown format '{format}' — expected 'pdf' or 'markdown'"}
    if fmt == "md":
        fmt = "markdown"

    recs = await _load_reservations()
    match = next((r for r in recs if _match_id(r, id)), None)
    if match is None:
        return {
            "error": f"no reservation matches id={id!r}",
            "hint": "try the full id like 'united-pnr:OSKNPT' or run list() first",
        }

    # Resolve output dir
    target_dir = Path(out_dir).expanduser() if out_dir else _pick_out_dir(match)
    target_dir.mkdir(parents=True, exist_ok=True)

    ext = "pdf" if fmt == "pdf" else "md"
    target_path = target_dir / _out_filename(match, ext)

    if fmt == "markdown":
        target_path.write_text(render_markdown_from(match), encoding="utf-8")
        return {
            "path": str(target_path),
            "format": "markdown",
            "pages": 1,
        }

    pdf = ItineraryPDF(match)
    pdf.render_all()
    # fpdf2's output(dest="F") was replaced — the modern API returns a
    # bytearray when no dest is passed. Write it ourselves for clarity.
    data = pdf.output()
    # Normalize to bytes — fpdf2 ≥ 2.7 returns bytearray.
    if isinstance(data, bytearray):
        data = bytes(data)
    target_path.write_bytes(data)

    return {
        "path": str(target_path),
        "format": "pdf",
        "pages": max(1, len(_trips(match))),
    }


@returns({"markdown": "string"})
@timeout(10)
async def render_markdown(reservation: dict | None = None, id: str | None = None, **_params) -> dict:
    """Pure helper. Returns markdown for a reservation.

    Accepts either:
      - `reservation`: a reservation dict (preferred — stateless)
      - `id`: look up the reservation by id (same matching as `render`).
    """
    if reservation is None and id:
        recs = await _load_reservations()
        reservation = next((r for r in recs if _match_id(r, id)), None)
    if not isinstance(reservation, dict):
        return {"error": "missing reservation — pass a dict or an id"}
    return {"markdown": render_markdown_from(reservation)}
