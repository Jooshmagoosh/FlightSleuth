# FlightSleuth

Finds the cheapest fares for a route. A tkinter window and a command line share one search module.
The brief is [docs/BUILD_PROMPT.md](docs/BUILD_PROMPT.md); progress is in [docs/ROADMAP.md](docs/ROADMAP.md).
When asked for the "next phase", read the roadmap, restate that phase's gate, then continue.

## Commands (PowerShell, from the repository root)

```
py -m unittest discover -s tests -t .        # all tests; no network, no key, no credits
py -m unittest tests.test_planner            # one module
py -m flightsleuth search JFK LHR --depart 2026-12-10 --nights 7 --dry-run   # plan only, free
py -m flightsleuth gui                       # the window (also: double-click "Start Flightsleuth.pyw")
py -m flightsleuth quota                     # searches left; free
```

## Rules

- **Never spend API credits without the owner's say-so.** Tests never touch the network. Anything
  that makes paid calls goes through `scan.plan` (free, used by `--dry-run`) and `scan.run`.
- **The core is standard library only**: `urllib`, `sqlite3`, `argparse`, `unittest`, `tkinter`.
  Third-party packages are allowed only in optional parts, and the core must import without them.
- **Extend, don't duplicate.** Grow `Request`, `Query`, `Offer`, `Plan` and `Outcome`; keep module names.
- **Every existing test stays green at every commit.** New behaviour gets `unittest` tests in the same style.
- **Change `gui.py` last** in any phase. Keep `README.md` and `HOW-TO-USE.txt` true.
- **Runs on Windows, macOS and Linux.** The owner develops on Windows in PowerShell.
- **No secrets in the repo.** The key lives in the environment or the git-ignored `.env`.
- **One branch per phase, small commits.** Never push to `main`; never merge a pull request.
- **Check current API documentation before coding against it.** Recorded replies live in `tests/fixtures`.
- If the brief conflicts with what the code needs, stop and ask.

## Hard limits

- No proxy rotation, CAPTCHA solving, fingerprint spoofing or checkout automation, ever.
  If a site blocks a request, disable that provider and say so.
- No airline or agency logos; plain-text names only.
- Hidden-city and self-transfer results are off by default and always labelled.
- Show the fetch time next to every price. Never present a saved or indicative price as live.

## Layout

| Module | Job |
| --- | --- |
| `scan.py` | `Request` -> `plan()` -> `run()`. The only place searches are planned, capped and paid for. |
| `planner.py` | Expands a request into date pairs. |
| `serpapi.py` | SerpApi HTTP client and reply parser. |
| `cache.py` | Disposable SQLite cache of raw replies (`cache.db`). |
| `models.py` | `Query`, `Offer`, `Insights`, `SearchResult`. |
| `render.py` | Wording shared by both front ends. |
| `cli.py`, `gui.py` | The two front ends. They hold no search logic. |
| `config.py` | Data folder, API key. |
| `tests/fakes.py` | Fake SerpApi, plus isolation from the real key, cache and `.env`. |
