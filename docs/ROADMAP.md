# Roadmap

One phase per session, one branch per phase. The full brief is [BUILD_PROMPT.md](BUILD_PROMPT.md).

## Phase 1, seams: accepted (branch `phase-1-seams`)

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
- [x] After acceptance: a one-way reply recorded through the real command line, and the
      `sort_by` question settled with one unsorted search

Not done: `pip install -e .` was not run, because it would download setuptools. The tests check
the file instead.

## Decisions the owner has made

- **`price_history` never goes into `price_points`.** When it is stored it gets its own table,
  with a unique key on the query and Google's date. It stays out of `history.db` until then.
- **`sort_by=2` stays.** The test was: drop it only if the unsorted reply has `price_insights`
  and still holds the cheapest fare. The first held and the second did not.

## Found in the real replies

These come from `tests/fixtures`; its README has the detail.

- **The sellers of one round trip cost three billed requests in all**: the search, the return
  flights, the booking options. That is the two extra the brief assumed. An empty reply is billed too.
- **A `departure_token` can return no flights.** It happened for the cheapest outbound.
- **The headline round-trip price can be two separate tickets.** The $694 fare was `separate_tickets`;
  the same flights on one ticket were $797. The catch is only visible in the booking options.
- **Sorting by price lists fares the default order leaves out, and they may not be real.** Without
  `sort_by` the reply dropped all 14 Scandinavian Airlines itineraries, including the ten cheapest,
  and its cheapest fare was $693 against $646. The $646 fare is the one whose follow-up was empty,
  and $693 is what Google itself reports as the lowest price.
- **The price rating depends on the request.** The sorted round trip carried no `price_insights`;
  the unsorted round trip, the one-way (sorted) and the booking options all did.
- **`price_history` is real**: one point a day for 61 days, in the unsorted round trip and in the
  one-way reply. So the running code already receives it on one-way searches.
- **One-way options carry a `booking_token` directly**, and an option-level
  `extensions: ["Checked baggage for a fee"]`. Nothing in any reply marks basic economy or
  self-transfer.
- **`cache.db` never drops expired replies.** They are ignored after six hours but stay on disk,
  and a real search reply takes 120 to 220 KB there. The owner's file had reached 12 MB by the end
  of Phase 1.

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

Requirements added after Phase 1:

- [ ] Never present a fare as the cheapest until its return flights have been fetched. If the
      follow-up is empty, fall through to the next fare and say so. Budget one spare call per
      cross-check.
- [ ] Label a fare sold as separate tickets, and show the one-ticket price beside it when the
      booking options give one.
- [ ] In the bake-off, measure whether `--split` ever beats the fare Google already lists.
- [ ] Remove expired replies from `cache.db`.
- [ ] Store `price_history` in its own table, as decided above. Real replies now contain it.

## Phase 3, second sources: not started

Gate: the mode test passes and each provider has recorded-response tests.

- [ ] Only sources the bake-off justifies: Skiplagged MCP, Kiwi MCP, Travelpayouts Data API,
      then optional `personal_only` providers

## Phase 4, watches and alerts: not started

Gate: a watch runs unattended for a week in a container and sends one true alert with no false ones.

## Phase 5, web app: not started

Gate: it installs to a phone home screen and receives a push alert.

## Phase 6, public readiness: only when asked
