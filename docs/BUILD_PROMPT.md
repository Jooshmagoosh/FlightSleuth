# FlightSleuth: extend the existing tool

You are working in the FlightSleuth repository. It finds the cheapest fares for a route by scanning dates, trip lengths and airports through SerpApi's Google Flights engine. A tkinter window and a command line share one search module.

I want it to grow into: searches over a true date window on a small call budget, more price sources, flexible-destination search, saved price watches with alerts, and later a hosted web app (a PWA). Today it serves me and a few friends. It may become a paid public product, so every data source must be labelled by whether a hosted product may use it.

Work through the phases below, one phase per session, and stop at the end of each for my review.

## What exists (checked at commit 8c40979; confirm before relying on it)

- Python 3.11 or newer, standard library only. No third-party packages, no `pyproject.toml`, no `CLAUDE.md`.
- `scan.py`: `Request` -> `plan()` -> `run()`. `plan()` expands the request into `Query` objects with `planner.date_pairs` and sorts them into saved and missing. `run()` constructs `SerpApi` directly, checks the account quota, fetches three at a time and caches the raw replies.
- `planner.py`: a full grid. `--flex N` gives 2N+1 departure dates, `--nights A-B` multiplies by the number of lengths, and `--split` adds one-way searches. One search per date pair; a comma-separated airport list rides in the same search.
- `serpapi.py`: request building, retries (never on HTTP 429), and parsing into `Offer`, `Insights` and `SearchResult`. An `Offer` describes only the outbound leg and keeps no flight numbers, tokens or seller. `price_history` is dropped. `gl` and `bags` are never sent.
- `cache.py`: one SQLite table of raw replies, reused for 6 hours and overwritten on refresh, so no price history survives.
- Guards: `--dry-run`, a 25-search cap per run (`DEFAULT_MAX_SEARCHES`), and a monthly and hourly quota check against SerpApi's account endpoint.
- `cli.py` (argparse: `search`, `gui`, `quota`, `cache`), `gui.py` (tkinter), `render.py` (wording shared by both).
- 65 `unittest` tests. `tests/fakes.py` replaces `scan.SerpApi` and isolates the key and the cache. Run them with `py -m unittest discover -s tests -t .`
- Known gaps: `tests/test_gui.py` fails on import where tkinter is missing, although the README says those tests are skipped. A 91-day departure window with trips of 5 to 9 nights needs 455 searches and is refused.

## How to work

1. Read the code first and tell me anything above that is wrong or out of date.
2. Never spend API credits without telling me. Tests keep running with the network off through `tests/fakes.py`. Every new command that makes paid calls goes through the existing dry run, cap and quota check.
3. Keep the core standard-library only: `urllib`, `sqlite3`, `argparse`, `unittest`. My friends install nothing but Python and double-click `Start Flightsleuth.pyw`. Third-party packages are allowed only in optional parts (the server, the web app, an MCP client), and the core must import without them.
4. Keep the existing module names and behaviour unless a phase says otherwise. Extend `Request`, `Query`, `Offer`, `Plan` and `Outcome`; do not build a parallel set beside them.
5. Every existing test stays green at every commit. Add tests in the same `unittest` style for everything new.
6. Keep the window working. Change `gui.py` last in any phase, and keep `HOW-TO-USE.txt` and `README.md` true.
7. Code must run on Windows, macOS and Linux. I develop on Windows in PowerShell, so give me commands that work there.
8. Keep secrets out of the repo. The key stays in the environment or the git-ignored `.env`.
9. Work on one branch per phase, in small commits.
10. Before coding against any external API, open its current documentation and confirm endpoint and parameter names. Your training data may be stale. The facts below were checked in October 2026.
11. If anything here conflicts with what the code needs, stop and ask me instead of guessing.
12. Create `CLAUDE.md` (architecture rules, commands) and `docs/ROADMAP.md` (phase checklist with status), and keep them current. When I say "next phase", read `docs/ROADMAP.md`, restate that phase's gate, and continue.

## Facts checked in October 2026

- SerpApi `engine=google_flights`, beyond what the code already sends: `gl` sets the country market, `bags` the number of carry-on bags, and `max_price` a ceiling. `price_insights` also carries `price_history`.
- SerpApi round trips: the first reply lists outbound flights, each with a `departure_token`, and the price shown is the round-trip total. A request with that `departure_token` returns the return flights. A request with a `booking_token` returns `booking_options`, one entry per seller and fare, each (under `together`) with `book_with`, `price` and an `airline` boolean. So the sellers of one round trip cost two more requests. Real replies recorded on 2026-10-09 (UTC) confirm the whole chain; `tests/fixtures/README.md` says what each one showed. The hand-written `synthetic_roundtrip.json` confirms nothing.
- SerpApi caching: a repeat of the same search within one hour is served from SerpApi's cache and is free. `no_cache=true` forces a fresh, paid fetch.
- SerpApi `engine=google_travel_explore`: `departure_id` is required. `arrival_area_id` targets a region and `arrival_id` one destination. Flexible dates use `month` (1 to 12, or 0 for the next six months) and `travel_duration` (1 weekend, 2 one week, 3 two weeks). Only the next six months are available. With `arrival_id` it returns a suggested `start_date`, `end_date` and flights.
- SerpApi plans: free is 250 searches a month, then $25 for 1,000 and $75 for 5,000. Treat every call as scarce.
- Amadeus Self-Service was shut down on July 17, 2026. Do not use it.
- Kiwi's Tequila API is invitation-only. Travelpayouts' real-time search API requires 50,000 monthly active users. Do not plan on either.
- Skiplagged runs a public MCP server at https://mcp.skiplagged.com/mcp with no key (docs: https://skiplagged.github.io/mcp/). Its tools include `sk_flights_search`, `sk_flex_departure_calendar`, `sk_flex_return_calendar` and `sk_destinations_anywhere`, and its prices are indicative. Kiwi.com runs one at https://mcp.kiwi.com with a `search-flight` tool, dates flexible by up to three days, and a booking link per result. Confirm both by listing their tools before writing adapters.
- Travelpayouts' Aviasales Data API is free with a token and returns cached prices two to seven days old. It defaults to the Russian market and roubles, so always pass `currency` and `market`.

## Target design

**Provider seam.** Add a `flightsleuth/providers/` package with one interface every source implements: `name`, `capabilities` (live search, date calendar, explore, return flights, booking options, deep link), `cost_per_call`, and `data_class`, which is one of `vendor`, `sanctioned` or `personal_only`. Put the SerpApi client behind it. `scan.run` asks a registry for its providers instead of constructing `SerpApi`. A setting `FLIGHTSLEUTH_MODE` is `personal` (the default) or `hosted`. In `hosted` mode the registry refuses to load any `personal_only` provider, and a test proves it.

**Richer offers.** `Offer` gains its segments (carrier, flight number, airports, local times), the return leg when known, seller, source, fetched-at time, `verified_live`, and flags for self-transfer, hidden-city, separate tickets and basic economy. Add an itinerary key built from the carrier, flight number and departure time of each segment, so the same flights from two sources match.

**Store.** Beside the `responses` cache table, add append-only tables with `sqlite3`: `price_points` (route, dates, price, currency, source, fetched-at) and `provider_calls` (provider, endpoint, cached or live, cost, latency, status). Keep the `price_history` SerpApi already returns.

**Search pipeline, in this order.**

1. Plan. `Request` gains a departure window (earliest and latest date) beside today's `--depart` with `--flex`. The planner chooses a scan strategy and reports its paid calls through the existing `Plan`, so the dry run, the cap and the quota check keep working. Strategies: explore-seeded (trip within six months and close to a weekend, one week or two weeks); sampled grid, then refine around the best dates; one-way decomposition (only for domestic or low-cost markets, where two one-ways equal a round trip); full grid (today's behaviour, for small windows).
2. Scout. Get candidate dates from the cheapest capable provider.
3. Verify. Fetch live prices for the candidates and their neighbours, with all requested airports in one call.
4. Cross-check the best results. Fetch the return flights (this also fixes today's outbound-only display), the booking options, one other market through `gl`, two one-ways through the existing `--split` path, and any enabled second source. Add deep links to Momondo, Skyscanner, Skiplagged and Kiwi beside the Google Flights link the code already shows. Keep the URL templates in one data file, because they change.
5. Rank by total cost with a bag setting. For each result, say whether it is the same flights from a cheaper seller or a different itinerary, and state the catch (self-transfer, hidden-city, unfamiliar agency, long layover).

Size the default pipeline to fit the 25-search cap: two seeds and one cross-check is about 24 calls. A `--thorough` option may use about 40.

**Hard limits.**

- No scraping code runs in `hosted` mode. `personal_only` providers fetch public, logged-out pages only, at human pace, from the user's own machine.
- Never add proxy rotation, CAPTCHA solving, fingerprint spoofing or any checkout automation. If a site blocks a request, mark that provider disabled and tell me.
- Never display airline or agency logos. Use plain-text names.
- Hidden-city and self-transfer results are off by default and always labelled.
- Show the fetch time next to every price. Never present a cached or indicative price as live.

## Phases

**Phase 1, seams.** Add the provider seam, the richer `Offer`, the two store tables and the run mode, with no change in what the window or the command line shows. Make `tests/test_gui.py` skip cleanly without tkinter. Update `tests/fakes.py` to plug the fake in through the registry. A `pyproject.toml` that adds a `flightsleuth` command is welcome if it adds no dependencies. Gate: all 65 existing tests pass, a fake second provider plugs in through the registry in a new test, the mode test passes, and the suite runs clean on a machine without tkinter.

**Phase 2, smarter search and measurement.** Implement the window planner and the pipeline, return flights, booking options, `gl`, `bags`, and an `explore` command on `google_travel_explore` for flexible destinations. Add `flightsleuth bakeoff`: for a file of routes and date windows, record the best price per source (automated where a provider exists; otherwise open the deep link and let me type the price), then report how often and by how much each source beats SerpApi, split into "same flights" and "different itinerary". Bring the window up to date last. Gate: the search that costs 455 calls today (91-day window, trips of 5 to 9 nights) dry-runs at 25 or fewer by default and about 40 with `--thorough`, and the bake-off report runs on my route file.

Phase 2 requirements added after Phase 1, from the real replies in `tests/fixtures`:

- Never present a fare as the cheapest until its return flights have been fetched. If the follow-up is empty, fall through to the next fare and say so. Budget one spare call per cross-check.
- Label a fare sold as separate tickets, and show the one-ticket price beside it when the booking options give one.
- In the bake-off, measure whether `--split` ever beats the fare Google already lists.
- Remove expired replies from `cache.db`.

**Phase 3, second sources.** Add providers only for sources the bake-off report justifies, in this order of preference: Skiplagged MCP, Kiwi MCP, Travelpayouts Data API, then optional `personal_only` providers (the `fli` library, a local browser checker). An MCP client may be an optional dependency. Gate: the mode test passes and each provider has recorded-response tests.

**Phase 4, watches and alerts.** Saved searches that re-run on a schedule, price history from `price_points`, alert rules (below a set price, a set percentage under the trailing median, below Google's typical range), and notifications through ntfy or Telegram first, then email. Watches on the same route and dates share one fetch. Always re-verify live before sending an alert. Gate: a watch runs unattended for a week in a container and sends one true alert with no false ones.

**Phase 5, web app.** A FastAPI server over the same core as an optional extra, invite-only sign-in, and a React PWA with search, explained results, explore, watches and web push. Gate: it installs to a phone home screen and receives a push alert.

**Phase 6, public readiness.** Only when I ask: hosted mode with licensed sources only, accounts, billing, affiliate links, terms and privacy pages, rate limits, and a Capacitor wrapper for the app stores if I want one.

## Start now

Read the code, then reply with: anything in "What exists" that is wrong, the decisions you need from me before Phase 1 (for example whether `serpapi.py` moves or stays where it is), and your Phase 1 plan as a short list of commits. Wait for my go-ahead, then do Phase 1 only.
