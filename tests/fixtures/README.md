# Fixtures

| File | Real? | What it is |
| --- | --- | --- |
| `synthetic_roundtrip.json` | **Synthetic** | Hand-written from SerpApi's documentation. Evidence of nothing; kept for the small parser tests built on it. |
| `roundtrip_outbound.json` | Real | Round trip JFK,EWR to LHR, 2026-12-08 to 2026-12-15, exactly as `serpapi.search_params` sends it. |
| `roundtrip_return_empty.json` | Real | Follow-up with the `departure_token` of the cheapest outbound ($646, SK 904 / SK 491 / SK 815). No flights came back. |
| `roundtrip_return.json` | Real | Follow-up with the `departure_token` of the nonstop UA 14 ($695). |
| `roundtrip_booking.json` | Real | Follow-up with the `booking_token` of the cheapest return (LX 345 / LX 18). |

The real replies were recorded on 2026-10-09 between 00:35 and 00:37 UTC, with `engine=google_flights`,
`sort_by=2`, `show_hidden=true`, `hl=en`, `currency=USD`. Each follow-up repeated the first request's
parameters and added the token.

## What they showed

- **Reaching the sellers of one round trip takes three requests**: the search, the return flights,
  the booking options. All four requests made here were billed, one search each (monthly usage went
  from 76 to 80), including the one that returned no flights.
- **Outbound reply**: 90 options, all in `other_flights`. With `sort_by=2` there was no
  `best_flights` and no `price_insights`. Every option has a `price` (the round-trip total) and a
  `departure_token`; none has a `booking_token`.
- **A `departure_token` can lead nowhere.** The cheapest outbound returned
  `"error": "Google Flights hasn't returned any results for this query."` with status `Success`.
- **Return reply**: 54 options, each with a `booking_token` and a `price` that is the round-trip
  total with the chosen outbound. The cheapest total matched the outbound's listed price.
- **Booking reply**: 12 `booking_options`, plus `selected_flights` (both legs), `baggage_prices`
  and `price_insights` (without `price_history`). The headline $694 was only available as
  **two separate tickets** (`separate_tickets: true`, with `departing` and `returning`). The same
  flights on one ticket cost $797 from United, $1,037 from SWISS or Lufthansa, and $1,041 or more
  from agencies.
- **Segments** carry `flight_number`, `airline`, `airplane`, `legroom`, `extensions`, and sometimes
  `overnight`, `ticket_also_sold_by`, `plane_and_crew_by` and `often_delayed_by_over_30_min`.
  No option carried option-level `extensions`, so nothing here marks basic economy or self-transfer.
- **`search_metadata`** has `created_at` and `processed_at` in UTC.

## Redaction

The account path in `search_metadata`'s archive links (`https://serpapi.com/searches/<path>/...`) was
replaced with `REDACTED`. Nothing else was changed. The files were checked for the API key before
they were committed.

## Not recorded yet

A one-way search, where options should carry a `booking_token` directly.
