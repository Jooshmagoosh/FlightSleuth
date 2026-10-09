# flightsleuth

Finds the cheapest fares for a route by scanning nearby dates, trip lengths and
airports. Prices come from Google Flights through [SerpApi](https://serpapi.com/google-flights-api),
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
and the cheapest flights appear below. The button under the result opens that
search on Google Flights in your browser.

The window covers the common options. Trip-length ranges (`--nights 6-8`), other
currencies, `--deep`, `--fresh` and `--dry-run` are command-line only.

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

Try trips of 6 to 8 nights, and check whether two one-way tickets beat the round trip:

```
py -m flightsleuth search JFK LHR --depart 2026-12-10 --nights 6-8 --flex 1 --split
```

See what a scan would cost before spending anything (needs no key):

```
py -m flightsleuth search JFK LHR --depart 2026-12-10 --nights 6-8 --flex 3 --dry-run
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

## How it stays inside the limits

- Results are saved locally for 6 hours (`--max-age`), so repeating or widening a scan only pays for the new dates.
- A run that needs more than 25 live searches is refused (`--max-searches` on the command line; fixed in the window).
- Before searching it asks SerpApi how many searches are left this month and this hour, and refuses a scan the account cannot cover.
- Searches run three at a time. A server error is retried twice with a growing wait.
- A "too many requests" reply is never retried. The run stops, no further search is sent, and what already finished stays saved.

## What it keeps on disk

Two files, in `%LOCALAPPDATA%\flightsleuth` on Windows and `~/.cache/flightsleuth` elsewhere
(set `FLIGHTSLEUTH_HOME` to move them):

- `cache.db` holds the saved replies. It is disposable; `cache --clear` empties it.
- `history.db` holds one price point for every search fetched live (route, dates, fare, source,
  fetch time) and a log of every call. Nothing in flightsleuth clears it, and its tables reject
  changes and deletions. Reusing a saved reply adds no price point.

## Price sources

Every source sits behind one interface in `flightsleuth/providers` and is labelled by how it may
be used: `vendor` (a paid API), `sanctioned` (offered by its owner for programs to call) or
`personal_only` (only ever from your own machine). SerpApi, labelled `vendor`, is the only source
today.

`FLIGHTSLEUTH_MODE` is `personal` by default. Set it to `hosted` when running flightsleuth for
other people: `personal_only` sources then refuse to load.

## Limits

- It sees what Google Flights sees. Airlines that do not list there will be missing.
- A round-trip price is the total, but only the outbound flight is shown. Use the printed link to pick the return.
- The cheapest round-trip price Google lists is sometimes only available as two separate tickets.
  Flightsleuth does not show that yet; the booking page behind the link does.
- Fares move. Treat a saved price as a lead and confirm it on the link before booking.

## Tests

```
py -m unittest discover -s tests -t .
```

The tests fake SerpApi's replies, so they need no key and spend no searches. They also run
from a throwaway folder, so your real `.env`, saved results and history are never touched. The
window tests open a hidden window and are skipped where tkinter is missing or there is no display.

`tests/fixtures` holds real SerpApi replies recorded in October 2026, and one hand-written one
that is labelled synthetic. Its README says how each was recorded and what it showed.
