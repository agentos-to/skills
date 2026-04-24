---
id: itineraries
name: Itineraries
description: Render travel itineraries (flights, lodging, rentals, dining) from graph `reservation` nodes — PDF, markdown, text.
color: '#1f5f99'
capabilities:
  - http
tools:
  list:
    async: false
  render:
    async: false
  render_markdown:
    async: false
---

# Itineraries

Render travel itineraries from the graph.

> **Provider-agnostic.** This skill reads the shared `reservation` shape
> (see `docs/shapes/reservation.yaml`). It does not know or care which
> provider skill wrote the reservation — United, Delta, Amtrak, Airbnb,
> OpenTable, etc. all emit the same shape; the same renderer works for
> all of them. If you're adding a new provider skill, your job stops
> at writing a graph-complete reservation; the rendering lives here.

## Tools

### `list(when="upcoming" | "past" | "all")`

Return graph reservations. Filters by time against the reservation's
`startTime` / `endTime`. Returns summary rows — id, provider, PNR, cities,
dates — suitable for showing the user a choice menu.

### `render(id, format="pdf" | "markdown", out_dir=None)`

Read one reservation by id (e.g. `united-pnr:OSKNPT`) and emit a file.

- **PDF** — produced by [fpdf2](https://py-pdf.github.io/fpdf2/). No
  system-level dependencies; cross-platform.
- **Markdown** — plain-text friendly, good for chat previews and emails.
- `out_dir` defaults to `~/Documents/Travel/<YYYY-MM <primary-city>>/`
  (creating the folder if missing). The filename convention is
  `<YYYY-MM-DD> <provider> <PNR> Itinerary.<ext>`.

### `render_markdown(reservation)`

Pure helper. Takes a reservation dict and returns a markdown string.
No I/O. Useful for previews, chat messages, email bodies.

## Design principles

1. **The shape is the interface.** The renderer only ever reads fields
   defined on `reservation` (and its relations: `trip`, `flight`,
   `airport`, `person`, `membership`, `place`). If a field you need
   isn't on the shape, add it to the shape — don't reach into
   `_raw.<provider-specific>` fields here.
2. **Graceful degradation.** If a field is missing, the renderer
   elides its section rather than erroring. A reservation with only
   PNR + dates still produces a valid (if minimal) PDF.
3. **Polish over complexity.** Single-page-per-trip layout, large
   airport codes as the visual anchor, calm typography, minimal color
   palette, subtle provider accent.
4. **One file per reservation.** Multi-trip reservations get multi-page
   PDFs, not multi-file outputs.
