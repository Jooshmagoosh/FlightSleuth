# FlightSleuth

Finds the cheapest fares for a route. A tkinter window and a command line share one search module.
It also watches up to three fares and alerts the owner's phone through ntfy when one drops.
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
py -m flightsleuth bakeoff docs/routes.txt --only 2 --dry-run                # one route's plan, free
py -m flightsleuth bakeoff --report          # what the last bake-off recorded; free
py -m flightsleuth watch add JFK LHR --depart 2026-12-08 --return 2026-12-15 --below 600 --dry-run   # the sums only; free
py -m flightsleuth watch list                # where each watch stands; free, reads history.db only
py -m flightsleuth watch run --dry-run       # what is due and the most it could cost; free
py -m flightsleuth gui                       # the window (also: double-click "Start Flightsleuth.pyw")
py -m flightsleuth quota                     # searches left; free
```

Everything else spends credits: any `search`, `explore` or `bakeoff` without `--dry-run`, and
`watch run`. `watch add` and `watch remove` are free but change the owner's real watches.
`watch test-alert` spends nothing but sends a real message to the owner's phone.
`watch setup` prints the secret topic: leave it to the owner.

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
  The ntfy topic is a secret of the same kind: whoever knows it can read every alert. It lives
  in `.env`, travels in a request's body and never in an address, and nothing but `watch setup`
  prints it. Do not read the owner's `.env`, and keep the topic out of logs, errors and tests.
- **Nobody but the owner schedules anything.** `watch run` checks what is due once and exits.
  The README gives the Task Scheduler command; the owner runs it.
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
  A price somebody found on another site never changes that line.
- No Momondo provider and no scraper of any kind, and no Kiwi, Travelpayouts or Skiplagged
  provider. The owner ruled this on 2026-10-09 from the bake-off. Other sites are reached by a
  link that a person opens, and what they read there is typed in.
- Never alert on a round trip until its return flights have been fetched, for that check and
  not from a saved reply. Never alert twice for the same price: a second alert needs a drop of
  at least $5 and at least 2% under the lowest price already alerted. The owner ruled both on
  2026-10-09, with the rest of what is under "Watches" below.
- At most three watches, and none that the searches cannot cover. The plan's numbers are read
  from the account every time; nothing about the free plan is written into the code.

## Layout

| Module | Job |
| --- | --- |
| `scan.py` | `Request` -> `plan()` -> `run()`. The only place calls are planned, capped, made and recorded. Also the staged window search and the closer look at the best fare, in full or as far as the sellers. |
| `planner.py` | Date arithmetic only: date pairs, window sampling, neighbours, explore months. Makes no calls. |
| `providers/` | One interface for every price source (`base.py`), the registry, and the SerpApi adapter. `Provider.allowance` reads what the account has left, its monthly total and its reset day. |
| `serpapi.py` | SerpApi HTTP client, request builders and reply parsers for both engines. Searches reach it only through its adapter. |
| `models.py` | `Query`, `Explore`, `Segment`, `Offer`, `Seller`, `Alternative`, `Check`, `Destination`, `Insights`, `SearchResult`. |
| `cache.py` | Disposable SQLite cache of raw replies (`cache.db`). Pruned after any run that fetched something. |
| `history.py` | Append-only tables in `history.db`. Never cleared. Four of them hold the watches. Each `add_*` builds a dict of column to value and hands it to `_add`; `rows(table)` reads one back. |
| `render.py` | Wording shared by both front ends, including the headline rules, every catch and the next step. Also the words of an alert, of a check's outcome, of `watch list` and of the budget's sums. |
| `watch.py` | Watches: the three rules, what counts as news, where a watch stands, the budget, and `run_due`, which checks what is due. Its searches go through `scan`; its alerts through `notify`. |
| `notify.py` | Sends one alert to ntfy. With `serpapi.py`, one of the only two modules that reach a network. |
| `links.py`, `links.json` | Addresses for the same dates on other sites. The patterns are data and say when they were checked. One site is marked `why_first`. |
| `typed.py` | Prices somebody read on another site and typed in: parsing, the questions, and the `site_prices` row. Reaches no network. |
| `bakeoff.py` | Route file, budget sharing, recording and the report. Its calls go through `scan`. |
| `cli.py`, `gui.py` | The two front ends. They hold no search logic. |
| `config.py` | Data folder, API key, run mode, and the settings kept beside the key: the ntfy topic and server, and the watches' monthly ceiling. |
| `tests/fakes.py` | Fake SerpApi registered in the real one's place, a fake ntfy, plus isolation from the real key, cache, history, `.env`, browser, keyboard and phone. `FakeSerpApi.rich` makes it reply as real replies do, with tokens; `FakeSerpApi.insights` sets what a listing says about its price. Also the helpers every test file shares: `run_cli`, `history_rows`, `endpoints`, `age_the_cache`, `NO_FLIGHTS` and `STAMP`. Add a helper here rather than copying one into a test file. |

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
- **Opening the stores.** `History()` and `Cache()` open their usual file and close themselves in a
  `with` block: `with History() as history:`. Only `scan._Session` holds one open across calls.
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
- **The next step.** After a round trip, confirmed or not, both front ends lead with
  `Check this fare on Momondo`, then Google's `Check and book`, then the other three sites.
  `Summary.next_step` holds it, with the fare a price found there is set against: the confirmed
  fare if there is one, else the listing. A one-way, and two one-ways that beat the round trip,
  have none and keep all four sites in `Summary.other_links`. The sentence shown with it is
  `why_first` in `links.json`: a dated measurement, to be replaced when it is measured again.
- **Typed prices.** `typed.record` writes one `site_prices` row: the price, the seller, whether it
  was the same flights, whether the seller's payment page showed it, and our fare with its fetch
  time and whether it was confirmed. The command line asks at the end of `search` when someone
  is at the keyboard (`--no-record` turns it off); the window has a button. A typed price goes
  nowhere else: no `price_points` row, no change to the headline. The window tests replace the
  clipboard in `GuiTest.setUp`, so a new window test class must reuse it.
- **Watches.** `watch.run_due` is the whole of an unattended run, and takes its clock as `now`.
  For each due watch: one search through `scan` (`by_price=False` for a round trip, the plain
  sorted search for a one-way), then `watch.fired` on the cheapest fare `scan.first_to_try`
  names. Only a price that fires and `watch.is_news` passes goes on to a second `scan` run with
  `look="confirm"`: the return flights and the sellers, never from a reply saved more than
  `CONFIRM_MINUTES` ago. The rules are tried again on the confirmed price before `notify.send`.
  - Nothing is stored about a watch but what happened. `watch.standings` works out the rest
    from the four tables each time: when it is next due, the lowest price alerted, the fares
    known to have no return flights (`Request.known_empty`), an alert still waiting to go.
  - A check that fails or is skipped is written with `finished = 0` and the watch stays due. A
    watch counts from the day of its last finished check, so a missed run is made up.
  - `watch.budget` sets the watches against `Provider.allowance`, with `HOLD` searches kept
    back for each. `watch.add` refuses what does not fit; a run spends only what the account,
    the monthly ceiling (`config.watch_monthly`) and its own `--max-searches` leave.
  - One check is a `watch._Check`: `run` makes the listing, `_judge` decides whether a price is
    worth an alert, `_confirm` fetches the return flights and the sellers, `_alert` sends it. Each
    step that ends the check calls `close`, which writes its `watch_checks` row.
  - `render.py` holds every word: `alert`, `outage`, `checked`, `watch_lines`, `budget_lines`, and
    `plural` for any count with its noun. It imports `watch` for type hints only, because `watch`
    imports it.
  - A round-trip check is not a plain search, so it writes no `price_points` row. A one-way's is,
    and does.
  - Both front ends add a watch through `watch.add`. The window does it off the main thread,
    because it asks the account what is left. Only the command line runs, lists and removes.
- **Pinned by tests, so work around rather than change:** the window's table headings and cell
  values, its detail text, the `price_points` columns, and the wording
  `N searches needed: X saved, Y live.` for a plan with nothing reserved. New window content goes
  in new widgets; the fetch time is the tree column `#0`. The window has two tabs, and a test
  pins that too. The owner changes pinned wording, as with the headline on 2026-10-08 and the
  Momondo sentence and link order on 2026-10-09; nobody else does. `GateTest` pins which modules
  may reach a network: `serpapi.py` and `notify.py`, and no third without the owner's say-so.
