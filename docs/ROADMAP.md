# Roadmap

One phase per session, one branch per phase. The full brief is [BUILD_PROMPT.md](BUILD_PROMPT.md).

## Phase 1, seams: in progress (branch `phase-1-seams`)

Gate: all 65 original tests pass, a fake second provider plugs in through the registry in a new
test, the mode test passes, and the suite runs clean on a machine without tkinter. Nothing the
window or the command line shows may change.

- [ ] `CLAUDE.md` and this roadmap
- [ ] Real SerpApi replies recorded as fixtures; the hand-written one labelled synthetic
- [ ] `tests/test_gui.py` skips cleanly without tkinter
- [ ] The per-run cap enforced inside `scan`
- [ ] `flightsleuth/providers/`: interface, registry, SerpApi adapter; fakes plug in through it
- [ ] `FLIGHTSLEUTH_MODE`: `hosted` refuses `personal_only` providers
- [ ] Richer `Offer`: segments, itinerary key, source, fetch time, flags
- [ ] `history.db`: append-only `price_points` and `provider_calls`
- [ ] `pyproject.toml` with a `flightsleuth` command and no dependencies
- [ ] `README.md` and `HOW-TO-USE.txt` brought up to date

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
