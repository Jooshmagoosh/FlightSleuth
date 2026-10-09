# Fixtures

| File | Real? | What it is |
| --- | --- | --- |
| `synthetic_roundtrip.json` | **Synthetic** | Hand-written from SerpApi's documentation. Evidence of nothing; kept for the small parser tests built on it. |
| `synthetic_explore.json` | **Synthetic** | A `google_travel_explore` reply with a list of places, hand-written. The London entry is the example on SerpApi's documentation page, checked on 2026-10-08; the Paris entry adds an `end_date`, which that page does not show. Evidence of nothing. |
| `explore_one_place_january.json` | Real | `google_travel_explore`, BOS to WAW, `month=1`, `travel_duration=2` (one week). Fetched by `py -m flightsleuth explore BOS --to WAW --month 1 --length week` and copied out of the cache. |
| `explore_one_place_february.json` | Real | The same with `month=2`. Together these are the two calls the `explore` strategy sends for the owner's BOS to WAW route. |
| `roundtrip_outbound.json` | Real | Round trip JFK,EWR to LHR, 2026-12-08 to 2026-12-15, exactly as `serpapi.search_params` sends it. |
| `roundtrip_return_empty.json` | Real | Follow-up with the `departure_token` of the cheapest outbound ($646, SK 904 / SK 491 / SK 815). No flights came back. |
| `roundtrip_return.json` | Real | Follow-up with the `departure_token` of the nonstop UA 14 ($695). |
| `roundtrip_booking.json` | Real | Follow-up with the `booking_token` of the cheapest return (LX 345 / LX 18). |
| `roundtrip_outbound_unsorted.json` | Real | The same round trip 29 minutes later, with `sort_by` left out. |
| `oneway.json` | Real | One way LAX to JFK, 2026-12-08, fetched by `py -m flightsleuth search LAX JFK --depart 2026-12-08 --one-way` and copied out of the cache. The first paid search through the Phase 1 code. |
| `watch_listing_unsorted.json` | Real | Round trip JFK,EWR to LHR, 2026-12-08 to 2026-12-15, with `sort_by` left out: the listing a watch check fetches. Fetched by `py -m flightsleuth watch run --max-searches 3` and copied out of the cache, as the next two were. |
| `watch_return.json` | Real | The follow-up that check made with the `departure_token` of the listing's cheapest fare ($754, FI 622 / FI 450). |
| `watch_booking.json` | Real | The follow-up it then made with the `booking_token` of the cheapest return (LX 345 / LX 18). |

The Google Flights replies were recorded on 2026-10-09 between 00:35 and 01:06 UTC, with `engine=google_flights`,
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

## What the explore replies showed

Recorded on 2026-10-09 at 02:53 and 02:54 UTC, one search credit each (monthly searches left went
from 168 to 166). Each request named one destination, `arrival_id=WAW`, as the `explore` strategy does.

- **A request for one place gets one pair of dates, and they are usable.** The reply has
  `start_date`, `end_date`, `flights`, `google_flights_link` and `google_flights_serpapi_link` at
  the top level, and no `destinations` list. This is what the brief said, and what SerpApi's
  documentation page does not show.
- **`month` steers the dates.** `month=1` gave 2027-01-25 to 2027-02-03; `month=2` gave 2027-02-01
  to 2027-02-10. Each start date is inside the month asked for; the two are a week apart.
- **"One week" was nine nights both times.** `travel_duration=2` is not seven nights.
- **`flights` is one entry per airline for those dates**, three or four of them, each with `price`,
  `airline`, `airline_code`, `number_of_stops`, `duration` and the two airports (`name` and `id`).
  The cheapest carries `cheapest_flight: true`. There are no flight numbers, times or tokens, so
  an explore price cannot be matched to an itinerary or followed up; a dated search has to do that.
- **`duration` is the flight out, in minutes**: 610 to 730 for the others. The cheapest January
  fare, $611, has 2215, which is 36 hours 55 minutes with one stop. The cheapest price explore
  gives can belong to a flight nobody would choose.
- **A joint fare names both airlines** as `"Delta, KLM"` with `airline_code: "multi"`.
- **`search_metadata` has `google_travel_explore_url`** in place of `google_flights_url`, and the
  same `processed_at`.

## What the watch check showed

Recorded on 2026-10-09 between 07:28:29 and 07:28:35 UTC (`processed_at`), by the one real check
Phase 4 made: a watch with a price set far above the fare so that it would fire. Three requests,
three searches billed (monthly searches left went from 31 to 28). These three belong together:
each follow-up used a token from the reply before it.

- **A `departure_token` from an unsorted listing fetches return flights.** Nothing recorded
  before had shown it. The first lookup worked, so the spare was not needed.
- **The listing**: 69 fares, 3 in `best_flights` and 66 in `other_flights`, every one with a
  `departure_token`. `price_insights` had `typical_price_range` 480 to 830 and a `price_history`
  of 62 daily lows.
- **Its cheapest fare was again the price Google calls its lowest**: $754 both, as $693 was both
  in `roundtrip_outbound_unsorted.json`. That is two unsorted replies out of two.
- **The same search priced $61 higher than six and a half hours earlier.** The earlier unsorted
  reply's cheapest was $693 (Aer Lingus); this one's was $754 (Icelandair).
- **The return reply** had 4 options, and the cheapest total was $754, the listed price.
- **The booking reply** had 3 sellers. The cheapest, $742 from "AranGrant, SWISS", was two
  separate tickets ($342 and $400). On one ticket it was $753 from Expedia and from Orbitz, both
  agencies; no airline sold it on one ticket. So one fare had three prices in three replies:
  $754 listed and confirmed, $742 as two tickets, $753 as one.
- **The booking reply's `price_insights`** gave a typical range of 540 to 1,850, not the
  listing's 480 to 830, and no `price_history`. The same was true of `roundtrip_booking.json`.
- **The booking lookup took 25 seconds**; the other two took under 3 each.

## What has not been recorded

- **No explore reply for a region or for anywhere**, which is what `flightsleuth explore --area`
  and a bare `flightsleuth explore` send. SerpApi's page documents a `destinations` list whose
  entries have `start_date`, `flight_price`, `flight_duration`, `number_of_stops`, `airline` and
  `destination_airport.code`, with no `end_date`. The parser reads that shape from the
  documentation alone.
- **No explore reply for several origins or for a weekend or two-week trip.**
- **No reply to a request that sent `gl` or `bags`.** Both are documented parameters, and every
  recorded reply reports `gl: us` for a request that sent none.
- **How long a token lasts.** The follow-ups here were made within half an hour of the search.

## Redaction

The account path in `search_metadata`'s archive links (`https://serpapi.com/searches/<path>/...`) was
replaced with `REDACTED`. Nothing else was changed. The files were checked for the API key before
they were committed. The two explore replies and the three watch replies were treated the same way.
