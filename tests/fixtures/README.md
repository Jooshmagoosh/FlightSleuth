# Fixtures

| File | Real? | What it is |
| --- | --- | --- |
| `synthetic_roundtrip.json` | **Synthetic** | Hand-written from SerpApi's documentation. Evidence of nothing; kept for the small parser tests built on it. |
| `synthetic_explore.json` | **Synthetic** | A `google_travel_explore` reply, hand-written. The London entry is the example on SerpApi's documentation page, checked on 2026-10-08; the Paris entry adds an `end_date`, which that page does not show. Evidence of nothing. |
| `roundtrip_outbound.json` | Real | Round trip JFK,EWR to LHR, 2026-12-08 to 2026-12-15, exactly as `serpapi.search_params` sends it. |
| `roundtrip_return_empty.json` | Real | Follow-up with the `departure_token` of the cheapest outbound ($646, SK 904 / SK 491 / SK 815). No flights came back. |
| `roundtrip_return.json` | Real | Follow-up with the `departure_token` of the nonstop UA 14 ($695). |
| `roundtrip_booking.json` | Real | Follow-up with the `booking_token` of the cheapest return (LX 345 / LX 18). |
| `roundtrip_outbound_unsorted.json` | Real | The same round trip 29 minutes later, with `sort_by` left out. |
| `oneway.json` | Real | One way LAX to JFK, 2026-12-08, fetched by `py -m flightsleuth search LAX JFK --depart 2026-12-08 --one-way` and copied out of the cache. The first paid search through the Phase 1 code. |

The real replies were recorded on 2026-10-09 between 00:35 and 01:06 UTC, with `engine=google_flights`,
`show_hidden=true`, `hl=en`, `currency=USD`, and `sort_by=2` except where noted. Each follow-up
repeated the first request's parameters and added the token.

## What they showed

- **Reaching the sellers of one round trip takes three requests**: the search, the return flights,
  the booking options. All six requests made here were billed, one search each (monthly usage went
  from 76 to 82), including the one that returned no flights.
- **Outbound reply**: 90 options, all in `other_flights`. There was no `best_flights` and no
  `price_insights`. Every option has a `price` (the round-trip total) and a `departure_token`;
  none has a `booking_token`.
- **A `departure_token` can lead nowhere.** The cheapest outbound returned
  `"error": "Google Flights hasn't returned any results for this query."` with status `Success`.
- **Return reply**: 54 options, each with a `booking_token` and a `price` that is the round-trip
  total with the chosen outbound. The cheapest total matched the outbound's listed price.
- **Booking reply**: 12 `booking_options`, plus `selected_flights` (both legs), `baggage_prices`
  and `price_insights` (without `price_history`). The headline $694 was only available as
  **two separate tickets** (`separate_tickets: true`, with `departing` and `returning`). The same
  flights on one ticket cost $797 from United, $1,037 from SWISS or Lufthansa, and $1,041 or more
  from agencies.
- **Leaving out `sort_by` trades the cheapest fares for the price rating.** The unsorted reply had
  5 `best_flights`, 72 `other_flights` and `price_insights` with a 61-day `price_history`. But 14
  itineraries from the sorted reply were missing, all Scandinavian Airlines, and they included
  every one of its ten cheapest ($646 to $661). The unsorted reply's cheapest fare was $693, which
  is also Google's `lowest_price` and the cheapest non-SAS fare in the sorted reply.
- **Those missing fares may not be bookable.** The one that was followed up, the $646, is the one
  whose `departure_token` returned no flights. One follow-up is not proof.
- **Seven itineraries were priced $20 to $78 higher in the unsorted reply** (British Airways,
  Lufthansa, LEVEL with Vueling), 29 minutes after the sorted one.
- **One-way reply**: 126 options, all in `other_flights`, each with a `booking_token` and no
  `departure_token`. Unlike the sorted round trip, it did carry `price_insights`, with
  `price_history`. Every option had `extensions: ["Checked baggage for a fee"]`.
- **`price_history`** is a list of `[seconds since the epoch, lowest price]`, one a day for 61 days.
- **Segments** carry `flight_number`, `airline`, `airplane`, `legroom`, `extensions`, and sometimes
  `overnight`, `ticket_also_sold_by`, `plane_and_crew_by` and `often_delayed_by_over_30_min`.
  Nothing in any reply marks basic economy or self-transfer.
- **`search_metadata`** has `created_at` and `processed_at` in UTC.

## What has not been recorded

- **No `google_travel_explore` reply.** SerpApi's page documents a `destinations` list whose entries
  have `start_date`, `flight_price`, `flight_duration`, `number_of_stops`, `airline` and
  `destination_airport.code`. It shows no `end_date`, and it does not show the reply for a request
  that names one `arrival_id`. The brief says that reply carries `start_date`, `end_date` and
  flights; nothing checked here confirms it. The parser reads both shapes and requires neither.
- **No reply to a request that sent `gl` or `bags`.** Both are documented parameters, and every
  recorded reply reports `gl: us` for a request that sent none.
- **How long a token lasts.** The follow-ups here were made within half an hour of the search.

## Redaction

The account path in `search_metadata`'s archive links (`https://serpapi.com/searches/<path>/...`) was
replaced with `REDACTED`. Nothing else was changed. The files were checked for the API key before
they were committed.
