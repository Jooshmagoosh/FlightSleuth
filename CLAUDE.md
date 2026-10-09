# FlightSleuth

Finds the cheapest fares for a route. A tkinter window and a command line share one search module.
The brief is [docs/BUILD_PROMPT.md](docs/BUILD_PROMPT.md); progress is in [docs/ROADMAP.md](docs/ROADMAP.md).
When asked for the "next phase", read the roadmap, restate that phase's gate, then continue.

## Commands (PowerShell, from the repository root)

```
py -m unittest discover -s tests -t .        # all tests; no network, no key, no credits
py -m unittest tests.test_scan               # one module
py -m flightsleuth search JFK LHR --depart 2026-12-10 --nights 7 --dry-run   # plan only, free
py -m flightsleuth gui                       # the window (also: double-click "Start Flightsleuth.pyw")
py -m flightsleuth quota                     # searches left; free
```

## Rules

- **Never spend API credits without the owner's say-so.** Tests never touch the network. Anything
  that makes paid calls goes through `scan.plan` (free, used by `--dry-run`) and `scan.run`, which
  enforces the per-run cap and each source's budget check.
- **The core is standard library only**: `urllib`, `sqlite3`, `argparse`, `unittest`, `tkinter`.
  Third-party packages are allowed only in optional parts, and the core must import without them.
  `tests/test_packaging.py` fails if a core module imports anything else.
- **Extend, don't duplicate.** Grow `Request`, `Query`, `Offer`, `Plan` and `Outcome`; keep module names.
- **Every existing test stays green at every commit.** New behaviour gets `unittest` tests in the same style.
- **Change `gui.py` last** in any phase. Keep `README.md` and `HOW-TO-USE.txt` true.
- **Runs on Windows, macOS and Linux.** The owner develops on Windows in PowerShell.
- **No secrets in the repo.** The key lives in the environment or the git-ignored `.env`.
  Recorded fixtures are checked for the key and have their account links redacted.
- **One branch per phase, small commits.** Never push to `main`; never merge a pull request.
- **Check current API documentation before coding against it.** Real replies live in `tests/fixtures`,
  and `tests/fixtures/README.md` records what they proved.
- If the brief conflicts with what the code needs, stop and ask.

## Hard limits

- No proxy rotation, CAPTCHA solving, fingerprint spoofing or checkout automation, ever.
  If a site blocks a request, disable that provider and say so.
- No scraping code runs in hosted mode. The registry enforces this through `data_class`.
- No airline or agency logos; plain-text names only.
- Hidden-city and self-transfer results are off by default and always labelled.
- Show the fetch time next to every price. Never present a saved or indicative price as live.

## Layout

| Module | Job |
| --- | --- |
| `scan.py` | `Request` -> `plan()` -> `run()`. The only place calls are planned, capped, made and recorded. |
| `planner.py` | Expands a request into date pairs. |
| `providers/` | One interface for every price source (`base.py`), the registry, and the SerpApi adapter. |
| `serpapi.py` | SerpApi HTTP client and reply parser. Searches reach it only through its adapter. |
| `models.py` | `Query`, `Segment`, `Offer`, `Insights`, `SearchResult`, `itinerary_key`. |
| `cache.py` | Disposable SQLite cache of raw replies (`cache.db`). |
| `history.py` | Append-only `price_points` and `provider_calls` (`history.db`). Never cleared. |
| `render.py` | Wording shared by both front ends. |
| `cli.py`, `gui.py` | The two front ends. They hold no search logic. |
| `config.py` | Data folder, API key, run mode. |
| `tests/fakes.py` | Fake SerpApi registered in the real one's place, plus isolation from the real key, cache, history and `.env`. |

## How the pieces fit

- **Sources.** Nothing outside `providers/` constructs a source. `scan` asks
  `providers.offering(Capability.LIVE_SEARCH)`; a plan is a list of `Call(provider, query)`.
  A new source subclasses `Provider`, sets `name`, `capabilities`, `cost_per_call` and `data_class`,
  and is added with `providers.register`. Registration fails without all four labels.
- **Mode.** `FLIGHTSLEUTH_MODE` is `personal` (default) or `hosted`. In `hosted` the registry will
  not load a `personal_only` source, even for a plan made earlier in personal mode.
- **Provenance.** Sources return offers without it; `scan` stamps `source`, `fetched_at` and
  `verified_live` on every offer. `fetched_at` is the time the source reports when it reports one
  (SerpApi's `processed_at`), else this machine's clock when the reply landed.
- **History.** A price point is written only when a reply is fetched live, as it lands. Replaying
  the cache writes a `provider_calls` row with `live = 0` and no price point. Planning, dry runs
  and refused runs write nothing.
- **Stopping.** After the first failed call no further call is sent; calls already in flight
  finish and are saved.
