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

Decided on 2026-10-08, after Phase 2 was built:

- **A round trip whose return flights have not been fetched is never printed as `Cheapest: $N`.**
  Both front ends print `Cheapest listed: $N round trip, <dates> (not yet confirmed)`. The Phase 1
  tests that pinned the old line were changed to pin this one.
- **The closer look stays off by default for `--depart`** and on for `--window`.
- **The spare return lookup goes to a different airline** than the one whose fare came back empty.
- **`auto` never picks the `one-way` or `explore` strategy.**
- **A search priced with `--bags` or `--market` writes no `price_points` row.**

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

## Phase 2, smarter search and measurement: built, awaiting review (branch `phase-2-search`)

Gate: the search that costs 455 calls today (91-day window, trips of 5 to 9 nights) dry-runs at
25 or fewer by default and about 40 with `--thorough`, and the bake-off report runs on the
owner's route file.

Where the gate stands:

- **Met:** the 455-pair search dry-runs at exactly 25, and at 40 with `--thorough`. `tests/test_window.py`
  and `tests/test_commands.py` pin both.
- **Waiting on the owner:** `bakeoff docs/routes.txt --dry-run` plans the six routes at up to 150
  live searches, and the report runs on what a bake-off records. The bake-off itself has not been
  run, because no credits were spent in this phase. Nothing was fetched from SerpApi at all.

- [x] Departure window on `Request`; planner strategies reported through `Plan`
- [x] Return flights and booking options (seller) through the token chain
- [x] `gl` and `bags`; ranking by total cost with the catch stated
- [x] `explore` command on `google_travel_explore` (built from the documentation; see "Not done")
- [x] Deep links to other sites, templates in one data file
- [x] `flightsleuth bakeoff`
- [x] Fetch time shown next to every price; the window brought up to date last

Requirements added after Phase 1:

- [x] Never present a fare as the cheapest until its return flights have been fetched. If the
      follow-up is empty, fall through to the next fare and say so. Budget one spare call per
      cross-check.
- [x] Label a fare sold as separate tickets, and show the one-ticket price beside it when the
      booking options give one.
- [x] In the bake-off, measure whether `--split` ever beats the fare Google already lists.
- [x] Remove expired replies from `cache.db`.
- [x] Store `price_history` in its own table, as decided above. Real replies now contain it.
- [x] In the bake-off, count how often the cheapest listed fare fails its return-flight lookup,
      and whether dropping `sort_by` would have avoided it.
- [ ] Decide `sort_by` from that count. Needs the bake-off to have been run.
- [ ] For Phase 4, evaluate an unsorted search as the light watch check: one call returned
      Google's lowest price, its rating and 61 days of price history.

### Decisions made while building

The owner has ruled on four of these; they are recorded under "Decisions the owner has made",
and the reasons are kept here.

- **The closer look is on for `--window` and off for `--depart`** (confirmed). Turning it on
  everywhere would add up to six searches to every simple search.
- **The spare return lookup goes to a different airline** (confirmed). The brief says "the next
  fare". In the recorded search the ten cheapest fares were all Scandinavian Airlines and the first
  had no return flights, so the next fare would probably have wasted the spare. The cheaper fares
  that were skipped are counted and reported as not looked up, and the headline then reads
  `Cheapest confirmed`.
- **`auto` never picks the `one-way` or `explore` strategy** (confirmed). One-way needs to know the
  market is one where two one-ways cost what a round trip does, which is what the bake-off's split
  measure is for. Both work when asked for by name.
- **`price_points` gets no row for a search priced with `--bags` or `--market`** (confirmed). The
  table has no column for either and a Phase 1 test pins its columns. Phase 4 will want to settle this.

Still for the owner to confirm or overturn:

- **A token is trusted for 60 minutes** (`scan.TOKEN_MINUTES`). Nothing recorded says how long one
  lasts. An older listing is fetched again before its tokens are followed, and the dry run counts that.
- **The other market is the destination's country**, read from the reply. A domestic route has
  none, so its closer look is one search cheaper.
- **The bake-off gives each route `min(25, budget / routes)` searches and keeps one of them back**
  for the unsorted probe, which is only made when a lookup fails. Six routes at 150 is 24 + 1 each.
- **The window shows the closer look in a second tab.** Stacked under the price lists it needed
  about 1,020 pixels of height.

### Not done

- **Nothing here has run against the live service.** Return flights, booking options and the
  countries are tested on the recorded replies. `gl`, `bags` and both explore shapes are tested
  only against the documentation.
- **`explore` is unverified.** SerpApi's page documents a `destinations` list with a `start_date`
  per place. It shows no `end_date` and no reply for a single `arrival_id`, which is what the
  `explore` strategy sends. Two recorded replies, one of each kind, would settle it: two credits.
- **The four site links were checked once, on 2026-10-08.** Momondo, Skiplagged and Kiwi.com
  opened the right search. Skyscanner named the right cities and then showed a robot check, so
  its dates were never seen. `links.json` records this per site.
- `pip install -e .` has still not been run.

### Found while building

- **SerpApi accepts both `gb` and `uk` for `gl`**, so the country code from a reply can be used as it is.
- **A `booking_token` request ignores the dates and filters sent with it**, according to the
  documentation. The code repeats the original search with the token, as the recorded request did.
- **The booking reply carries `price_insights`**, which the sorted search does not. A confirmed
  fare therefore gets Google's price rating back for nothing.
- **Kiwi.com's link is `/deep?from=&to=&departure=&return=`.** The `/search/results/JFK/LHR/...`
  form opens an empty search.
- **A sampled window prices about 19 of its pairs.** On a made-up fare landscape with one best
  pair, refining reached a pair $10 from the best. That says the stages work, not how good the
  fares it finds on real routes are; the bake-off's baseline is the first real measure of that.

## Phase 3, second sources: not started

Gate: the mode test passes and each provider has recorded-response tests.

- [ ] Only sources the bake-off justifies: Skiplagged MCP, Kiwi MCP, Travelpayouts Data API,
      then optional `personal_only` providers

## Phase 4, watches and alerts: not started

Gate: a watch runs unattended for a week in a container and sends one true alert with no false ones.

## Phase 5, web app: not started

Gate: it installs to a phone home screen and receives a push alert.

## Phase 6, public readiness: only when asked
