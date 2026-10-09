# FlightSleuth

Finds the cheapest fares for a route. A tkinter window and a command line share one search module.
The brief is [docs/BUILD_PROMPT.md](docs/BUILD_PROMPT.md); progress is in [docs/ROADMAP.md](docs/ROADMAP.md).
When asked for the "next phase", read the roadmap, restate that phase's gate, then continue.

## Commands (PowerShell, from the repository root)

```
py -m unittest discover -s tests -t .        # all tests; no network, no key, no credits
py -m unittest tests.test_scan               # one module
py -m flightsleuth search JFK LHR --depart 2026-12-10 --nights 7 --dry-run   # plan only, free
py -m flightsleuth search JFK LHR --window 2026-12-01 2027-03-01 --nights 5-9 --dry-run   # the gate search
py -m flightsleuth explore MCO --area europe --month 3 --dry-run             # plan only, free
py -m flightsleuth bakeoff docs/routes.txt --dry-run                         # plan only, free
py -m flightsleuth bakeoff --report          # what the last bake-off recorded; free
py -m flightsleuth gui                       # the window (also: double-click "Start Flightsleuth.pyw")
py -m flightsleuth quota                     # searches left; free
```

Everything else spends credits: any `search`, `explore` or `bakeoff` without `--dry-run`.

## Rules

- **Never spend API credits without the owner's say-so.** Tests never touch the network. Anything
  that makes paid calls goes through `scan.plan` (free, used by `--dry-run`) and `scan.run`, which
  enforces the per-run cap and each source's budget check.
- **The core is standard library only**: `urllib`, `sqlite3`, `argparse`, `unittest`, `tkinter`.
  Third-party packages are allowed only in optional parts, and the core must import without them.
  `tests/test_packaging.py` fails if a core module imports anything else.
- **Extend, don't duplicate.** Grow `Request`, `Query`, `Offer`, `Plan` and `Outcome`; keep module names.
- **Every existing test stays green at every commit.** New behaviour gets `unittest` tests in the same style.
  Run the suite and read its last line before committing; do not chain the commit to a command that hides it.
- **Change `gui.py` last** in any phase. Keep `README.md` and `HOW-TO-USE.txt` true.
- **Runs on Windows, macOS and Linux.** The owner develops on Windows in PowerShell.
- **No secrets in the repo.** The key lives in the environment or the git-ignored `.env`.
  Recorded fixtures are checked for the key and have their account links redacted.
- **One branch per phase, small commits.** Never push to `main`; never merge a pull request.
- **Check current API documentation before coding against it.** Real replies live in `tests/fixtures`,
  and `tests/fixtures/README.md` records what they proved and what nothing has proved yet.
- If the brief conflicts with what the code needs, stop and ask.

## Hard limits

- No proxy rotation, CAPTCHA solving, fingerprint spoofing or checkout automation, ever.
  If a site blocks a request, disable that provider and say so.
- No scraping code runs in hosted mode. The registry enforces this through `data_class`.
- No airline or agency logos; plain-text names only.
- Hidden-city and self-transfer results are off by default and always labelled.
  `scan` drops them unless `Request.risky` is set; `render.catches` words the label.
- Show the fetch time next to every price. Never present a saved or indicative price as live.
  `render.fetched` words it; a saved price reads "(saved)".
- Never call a round-trip fare the cheapest until its return flights have been fetched. Until
  then both front ends print `Cheapest listed: $N ... (not yet confirmed)`. `Summary.headline`
  and `Summary.caveat` hold that wording, and the tests pin all four forms of the line.

## Layout

| Module | Job |
| --- | --- |
| `scan.py` | `Request` -> `plan()` -> `run()`. The only place calls are planned, capped, made and recorded. Also the staged window search and the closer look at the best fare. |
| `planner.py` | Date arithmetic only: date pairs, window sampling, neighbours, explore months. Makes no calls. |
| `providers/` | One interface for every price source (`base.py`), the registry, and the SerpApi adapter. |
| `serpapi.py` | SerpApi HTTP client, request builders and reply parsers for both engines. Searches reach it only through its adapter. |
| `models.py` | `Query`, `Explore`, `Segment`, `Offer`, `Seller`, `Alternative`, `Check`, `Destination`, `Insights`, `SearchResult`. |
| `cache.py` | Disposable SQLite cache of raw replies (`cache.db`). Pruned after any run that fetched something. |
| `history.py` | Append-only tables in `history.db`. Never cleared. |
| `render.py` | Wording shared by both front ends, including the headline rules and every catch. |
| `links.py`, `links.json` | Addresses for the same dates on other sites. The patterns are data and say when they were checked. |
| `bakeoff.py` | Route file, budget sharing, recording and the report. Its calls go through `scan`. |
| `cli.py`, `gui.py` | The two front ends. They hold no search logic. |
| `config.py` | Data folder, API key, run mode. |
| `tests/fakes.py` | Fake SerpApi registered in the real one's place, plus isolation from the real key, cache, history, `.env`, browser and keyboard. `FakeSerpApi.rich` makes it reply as real replies do, with tokens. |

## How the pieces fit

- **Sources.** Nothing outside `providers/` constructs a source. `scan` asks
  `providers.offering(Capability.LIVE_SEARCH)`; a plan is a list of `Call(provider, query)`.
  A new source subclasses `Provider`, sets `name`, `capabilities`, `cost_per_call` and `data_class`,
  and is added with `providers.register`. Registration fails without all four labels.
- **One call shape.** `Provider.search` takes a `Query` or an `Explore`. A `Query` with a
  `departure_token` asks for return flights, one with a `booking_token` for sellers, one with a
  `market` for another country's prices. `scan` only sends a source what its capabilities cover.
  Each extra parameter is sent only when set, so an ordinary search keeps its cache key.
- **Mode.** `FLIGHTSLEUTH_MODE` is `personal` (default) or `hosted`. In `hosted` the registry will
  not load a `personal_only` source, even for a plan made earlier in personal mode.
- **Stages and the reserve.** A window too wide to price in full is sampled, refined, then looked
  at closely. Later stages depend on earlier replies, so their calls cannot be listed in advance.
  `Plan.reserved` counts the most each source may still be sent, and `Plan.live` includes it. The
  dry run, `check_cap` and each source's budget check all use that ceiling; `run` never exceeds it.
  A plan with nothing reserved behaves exactly as before Phase 2.
- **The closer look** (`scan._check`): return flights for the cheapest fare, one spare lookup for a
  different airline if that comes back empty, then the sellers, one other market and the two
  one-ways. It is on for a window and off otherwise (`Request.check`). A failed fare is removed from
  the results; the confirmed one replaces its listing.
- **Tokens go stale.** A follow-up is only sent live while its listing is under `TOKEN_MINUTES`
  old; otherwise the listing is fetched again first. A run that is wholly saved replays for nothing.
- **Provenance.** Sources return offers without it; `scan` stamps `source`, `fetched_at` and
  `verified_live` on every offer. `fetched_at` is the time the source reports when it reports one
  (SerpApi's `processed_at`), else this machine's clock when the reply landed.
- **History.** A price point is written only when a plain search is fetched live, as it lands.
  Plain means no market, no bags and sorted by price: `price_points` has no column for the others,
  and a test pins its columns. Follow-ups write a `provider_calls` row under their own endpoint and
  no price point. Replaying the cache writes a `provider_calls` row with `live = 0`. Planning, dry
  runs and refused runs write nothing. Google's `price_history` goes only into
  `google_price_history`, with a unique key on the query and Google's date.
- **Sorting.** Requests keep `sort_by=2`. Without it the reply gains `price_insights` but loses
  the cheapest fares; `tests/test_serpapi.py` pins that on two real replies. `Request.by_price`
  exists for the bake-off's probe, which asks whether Google's own order lists a fare that failed.
- **Stopping.** After the first failed call no further call is sent; calls already in flight
  finish and are saved.
- **Pinned by tests, so work around rather than change:** the window's table headings and cell
  values, its detail text, the `price_points` columns, and the wording
  `N searches needed: X saved, Y live.` for a plan with nothing reserved. New window content goes
  in new widgets; the fetch time is the tree column `#0`. The owner changes pinned wording, as
  with the headline on 2026-10-08; nobody else does.
