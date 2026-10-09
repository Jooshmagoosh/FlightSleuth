# Roadmap

One phase per session, one branch per phase. The full brief is [BUILD_PROMPT.md](BUILD_PROMPT.md).

## Phase 1, seams: built, awaiting review (branch `phase-1-seams`)

Gate: all 65 original tests pass, a fake second provider plugs in through the registry in a new
test, the mode test passes, and the suite runs clean on a machine without tkinter. Nothing the
window or the command line shows may change.

- [x] `CLAUDE.md` and this roadmap
- [x] Real SerpApi replies recorded as fixtures; the hand-written one labelled synthetic
- [x] `tests/test_gui.py` skips cleanly without tkinter
- [x] The per-run cap enforced inside `scan`
- [x] `flightsleuth/providers/`: interface, registry, SerpApi adapter; fakes plug in through it
- [x] `FLIGHTSLEUTH_MODE`: `hosted` refuses `personal_only` providers
- [x] Richer `Offer`: segments, itinerary key, source, fetch time, flags
- [x] `history.db`: append-only `price_points` and `provider_calls`
- [x] `pyproject.toml` with a `flightsleuth` command and no dependencies
- [x] `README.md` and `HOW-TO-USE.txt` brought up to date

Not done in this phase:

- A one-way reply was not recorded. One of the four approved searches went on a `departure_token`
  that returned no flights, and the chain to the booking options took priority.
- `pip install -e .` was not run; it would download setuptools. The tests check the file instead.

## Found while recording, for Phase 2 to act on

These come from the real replies in `tests/fixtures`; its README has the detail.

- **The sellers of one round trip cost three billed requests in all**: the search, the return
  flights, the booking options. That is the two extra the brief assumed. An empty reply is billed too.
- **A `departure_token` can return no flights.** It happened for the cheapest outbound, so the
  cross-check step must budget for a wasted call or pick its outbound with care.
- **The headline round-trip price can be two separate tickets.** The $694 fare was `separate_tickets`;
  the same flights on one ticket were $797. The catch is only visible in the booking options.
- **`sort_by=2` replies carry no `price_insights` and no `best_flights`.** So the "Google rates this
  price" line never appears on a real search today. The booking-options reply did carry
  `price_insights`, without `price_history`.
- **No reply marked basic economy or self-transfer.** Those flags need a source that states them.
- **`price_history` has not been seen in a real reply.** It is parsed and kept on `Insights`, from
  the documentation's shape only.
- **`cache.db` never drops expired replies.** They are ignored after six hours but stay on disk,
  and a real reply is about 200 KB. The owner's file had reached 12 MB by the end of Phase 1.

## Open questions for the owner

- Should Google's `price_history` be written into `history.db`? It was held back because those
  points carry Google's dates rather than a fetch time of ours, and repeat on every fetch.
- Should `sort_by=2` stay? It finds the cheapest fares but costs the price rating.
- Spend one more search on a one-way fixture?

## Phase 2, smarter search and measurement: not started

Gate: the search that costs 455 calls today (91-day window, trips of 5 to 9 nights) dry-runs at
25 or fewer by default and about 40 with `--thorough`, and the bake-off report runs on the
owner's route file.

- [ ] Departure window on `Request`; planner strategies reported through `Plan`
- [ ] Return flights and booking options (seller) through the token chain
- [ ] `gl` and `bags`; ranking by total cost with the catch stated
- [ ] `explore` command on `google_travel_explore`
- [ ] Deep links to other sites, templates in one data file
- [ ] `flightsleuth bakeoff`
- [ ] Fetch time shown next to every price; the window brought up to date last

## Phase 3, second sources: not started

Gate: the mode test passes and each provider has recorded-response tests.

- [ ] Only sources the bake-off justifies: Skiplagged MCP, Kiwi MCP, Travelpayouts Data API,
      then optional `personal_only` providers

## Phase 4, watches and alerts: not started

Gate: a watch runs unattended for a week in a container and sends one true alert with no false ones.

## Phase 5, web app: not started

Gate: it installs to a phone home screen and receives a push alert.

## Phase 6, public readiness: only when asked
