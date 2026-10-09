# flightsleuth

Finds the cheapest fares for a route by scanning dates, trip lengths and airports, then looks
closer at the best one: its return flights, who sells it, and other ways to fly the same dates.
Prices come from Google Flights through [SerpApi](https://serpapi.com/google-flights-api),
a paid API that permits automated access, so nothing here scrapes airline sites.

Needs Python 3.11 or newer. No other packages.

It has two front ends over the same search: a window and a command line.
[HOW-TO-USE.txt](HOW-TO-USE.txt) walks a beginner through the window.

## Setup

1. Create a SerpApi account and copy your key from <https://serpapi.com/manage-api-key>.
   The free plan gives 250 searches a month.
2. Give flightsleuth the key in any one of these ways:
   - Open the window and paste it when asked (or use the "SerpApi key..." button).
   - Copy `.env.example` to `.env` and paste the key in.
   - Set `SERPAPI_API_KEY` in your environment.

## The window

Double-click `Start Flightsleuth.pyw`, or run `py -m flightsleuth gui` from this folder.

Fill in the airports and dates, press Search, and the cheapest price for each date
and the cheapest flights appear below. The first column of every list says when that
price was fetched. The button under the result opens that search on Google Flights in
your browser, and the row below it opens the same dates on four other sites.

Tick "Or search a whole range" to leave on any day between two dates, staying a span of
nights. Tick "Confirm the best fare" to have the return flights and the sellers looked up;
a range does that by itself. What it finds goes in the second tab.

Other currencies, another country's prices, `--thorough`, `--strategy`, `--deep`, `--fresh`,
`--dry-run`, `explore` and `bakeoff` are command-line only.

## The command line

Run these from this folder.

One fixed round trip:

```
py -m flightsleuth search JFK LHR --depart 2026-12-10 --return 2026-12-17
```

Slide the trip three days either side, from any New York airport:

```
py -m flightsleuth search JFK,EWR,LGA LHR,LGW --depart 2026-12-10 --return 2026-12-17 --flex 3
```

Leave on any day in three months, for 5 to 9 nights. This is 455 date pairs, and it is
covered in 25 searches or fewer:

```
py -m flightsleuth search JFK LHR --window 2026-12-01 2027-03-01 --nights 5-9
```

Confirm a fixed-date fare before trusting it, with one carry-on bag in every price:

```
py -m flightsleuth search JFK LHR --depart 2026-12-10 --return 2026-12-17 --check --bags 1
```

See what any of these would cost before spending anything (needs no key):

```
py -m flightsleuth search JFK LHR --window 2026-12-01 2027-03-01 --nights 5-9 --dry-run
```

Find cheap places to go in March when the destination is open:

```
py -m flightsleuth explore MCO,SFB --area europe --month 3 --length week
```

Other commands: `py -m flightsleuth quota` shows searches left, `py -m flightsleuth cache`
shows saved results and `--clear` empties them. `py -m flightsleuth search --help` lists every option.

To type `flightsleuth` instead of `py -m flightsleuth`, install it once from this folder with
`py -m pip install -e .`. This is optional and adds no other packages.

## What a scan costs

Each date pair is one search. Listing several airports is free: they go into the same search.

| Option | Searches |
| --- | --- |
| `--flex N` | 2N + 1 departure dates |
| `--nights A-B` | multiplies by the number of lengths |
| `--split` | adds one one-way search per distinct departure date and per distinct return date |
| `--check` | up to 6 more for a round trip, 2 for a one-way |
| `--window FIRST LAST` | every date pair if that fits the limit; otherwise the limit, never more |

### A date window

When pricing every pair in a window would go over the limit, the window is covered in stages:

1. **Sample.** Twelve pairs spread across the window, so every weekday and every trip length gets a turn.
2. **Refine.** Up to seven more, a day either side of the cheapest pairs found so far.
3. **Look closer** at the best fare, as `--check` does. Up to six.

The later stages depend on what the earlier replies say, so a dry run can only give a ceiling:
"up to 25". `--thorough` raises the limit to 40, prices more dates and looks closer at the two
best fares. `--strategy` picks another way to cover the window:

| Strategy | What it does |
| --- | --- |
| `auto` | Every pair if that fits the limit, else `sampled`. The default. |
| `grid` | Every pair, as `--depart` with `--flex` and `--nights` always did. |
| `sampled` | The three stages above. |
| `one-way` | Prices every day out and every day back as one-ways, then searches the cheapest three sums as real round trips. For markets where two one-ways cost what a round trip does. |
| `explore` | Asks Google's explore engine for the cheapest dates in each month, then prices those and the days around them. Not yet confirmed against a real reply; see Limits. |

### The closer look

No round-trip fare is called the cheapest until its return flights have been fetched. `--check` does this:

1. Asks for the return flights of the cheapest fare. If none come back, that fare is dropped and
   named, and one spare lookup goes to the cheapest fare flown by a different airline.
2. Asks who sells the confirmed fare. A fare sold as two separate tickets is labelled, with the
   one-ticket price beside it.
3. Prices the same dates as sold in the destination's country (`--other-market` names another).
4. Prices the two one-ways.

Each of these is set beside the confirmed fare as "same flights" or "different itinerary", with its catch:
separate tickets, an agency rather than the airline, a layover of five hours or more.

It is on for `--window` and off otherwise. The line under the tables says which you got:

| Line | Meaning |
| --- | --- |
| `Cheapest listed: $455 round trip, ... (not yet confirmed)` | A round trip nobody looked up. The output says how to confirm it. |
| `Cheapest: $455 round trip, ...` | Its return flights were fetched. A one-way always reads this way. |
| `Cheapest confirmed: $555 round trip, ...` | Confirmed, but cheaper fares are listed that were not looked up. |
| `Cheapest listed: $550 round trip, ... (not confirmed)` | The closer look tried and no fare held up. |

## How it stays inside the limits

- Results are saved locally for 6 hours (`--max-age`), so repeating or widening a scan only pays for the new dates.
  Replies older than that are removed from `cache.db` the next time a search fetches anything.
- A run that needs more than 25 live searches is refused (`--max-searches` on the command line; fixed in the window).
  A staged search is sized to the limit and never sends a source more than the dry run counted for it.
- Before searching it asks SerpApi how many searches are left this month and this hour, and refuses a scan the account cannot cover.
- Searches run three at a time. A server error is retried twice with a growing wait.
- A "too many requests" reply is never retried. The run stops, no further search is sent, and what already finished stays saved.
- A return or seller lookup is only sent while the listing it comes from is under an hour old.
  An older listing is fetched again first, which the dry run counts.

## The bake-off

`bakeoff` measures other ways of finding a fare against SerpApi's own, on a file of routes:

```
py -m flightsleuth bakeoff docs/routes.txt --dry-run
py -m flightsleuth bakeoff docs/routes.txt --only 2
py -m flightsleuth bakeoff docs/routes.txt
py -m flightsleuth bakeoff --report
```

A route is one line: `MCO,SFB to JFK,EWR | depart between 2026-11-10 and 2026-12-20 | 2 to 4 nights`.
The length may also be `7 nights` or `one way`. The first six routes are used unless `--routes N` says otherwise.
`--only N` runs just the Nth route in the file, counting from 1: up to 25 searches, to try one
route before paying for them all. Run within six hours, the full bake-off then replays that route for nothing.

Each route gets a window search with the closer look. `--budget` (150 by default) is shared out
evenly, and no route gets more than one run's limit. It asks before spending unless `--yes` is given.
For each route it records, in `history.db`:

- the confirmed fare, as the baseline;
- two one-way tickets, and the destination country's price;
- the one-ticket price, when the cheapest fare is two separate tickets;
- whether the cheapest listed fare had return flights, and, when it had none, whether a search in
  Google's own order (no `sort_by`) would have listed that fare at all. That costs one more search;
- for Momondo, Skyscanner, Skiplagged and Kiwi.com, which have no provider yet: it opens the same
  dates in your browser and you type the best price you see. `--no-manual` skips this.

The report says how often and by how much each one beat SerpApi, split into the same flights
and a different itinerary. If a limit stops it part-way, run the same command again: finished
routes replay from saved results.

## Other sites

Links to the same dates on Momondo, Skyscanner, Skiplagged and Kiwi.com are built from
`flightsleuth/links.json`. These are address patterns, not APIs, and they change; each entry
says when it was last checked and how far. Skiplagged's and Kiwi.com's links say that those
sites list hidden-city and self-transfer fares.

## What it keeps on disk

Two files, in `%LOCALAPPDATA%\flightsleuth` on Windows and `~/.cache/flightsleuth` elsewhere
(set `FLIGHTSLEUTH_HOME` to move them):

- `cache.db` holds the saved replies. It is disposable; `cache --clear` empties it.
- `history.db` is kept. Nothing in flightsleuth clears it, and its tables reject changes and deletions.
  - `price_points`: one row for every search fetched live (route, dates, fare, source, fetch time).
    Reusing a saved reply adds none. Searches priced with `--bags` or `--market` add none either,
    because the table cannot say which they were.
  - `provider_calls`: every call, live or saved, with what it was for.
  - `google_price_history`: Google's own daily lowest price, when a reply carries it. One row a day per search.
  - `bakeoff_fares` and `bakeoff_lookups`: what each bake-off found.

## Price sources

Every source sits behind one interface in `flightsleuth/providers` and is labelled by how it may
be used: `vendor` (a paid API), `sanctioned` (offered by its owner for programs to call) or
`personal_only` (only ever from your own machine). SerpApi, labelled `vendor`, is the only source
today.

`FLIGHTSLEUTH_MODE` is `personal` by default. Set it to `hosted` when running flightsleuth for
other people: `personal_only` sources then refuse to load.

Hidden-city and self-transfer fares are left out unless `--risky` asks for them, and are always labelled.
No source marks one today.

## Limits

- It sees what Google Flights sees. Airlines that do not list there will be missing.
- Without `--check`, a round-trip price is the total but only the outbound flight is shown, and
  nobody has confirmed that return flights exist at that price. In one recorded search the
  cheapest fare listed had none.
- A sampled window prices about 19 of its date pairs. It finds a good fare, not provably the best.
- The spare return lookup goes to a different airline. Cheaper fares from the airline that came
  back empty stay listed, and the output says how many were not looked up.
- The other country's price is a listing: its return flights are not looked up.
- `explore` and the `explore` strategy are built from SerpApi's documentation. That documentation
  shows a list of places with a start date and no end date, and does not show the reply for one
  named airport. No real reply has been recorded yet, so treat both as untested against the live service.
- Fares move. Treat a saved price as a lead and confirm it on the link before booking.

## Tests

```
py -m unittest discover -s tests -t .
```

The tests fake SerpApi's replies, so they need no key and spend no searches. They also run
from a throwaway folder, so your real `.env`, saved results and history are never touched, and
they cannot open a browser. The window tests open a hidden window and are skipped where tkinter
is missing or there is no display.

`tests/fixtures` holds real SerpApi replies recorded in October 2026, and two hand-written ones
that are labelled synthetic. Its README says how each was recorded and what it showed.
