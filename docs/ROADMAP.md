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

Decided on 2026-10-09, from the bake-off results below:

- **Phase 3 is rescoped** to the three items listed under it. They replace the brief's
  "Phase 3, second sources".
- **No Momondo provider and no scraper of any kind.** Momondo is reached by its link, opened by a
  person, and the price found there is typed in.
- **No Kiwi, Travelpayouts or Skiplagged provider.**
- **Phase 3 needs no searches**, so it spends no credits.
- **`sort_by=2` stays**, now on the bake-off's count as well. The cheapest listed fare had return
  flights on all 6 routes, and the closer look covers the rare miss.
- **Momondo is promoted on every round trip, confirmed or not.** Under a fare that is not
  confirmed it also says that checking it on Momondo is a free way to confirm it. One-way results
  stay as they were.
- **The order is Momondo, then Google's "Check and book", then the other three sites.**
- **The sentence shown with it**: "On 8 Oct 2026 Momondo listed the same flights for less on 5 of
  6 test routes. The two largest savings, $97 and $51, were on long trips."
- **The command line asks for the price found at the end of a search**, as the bake-off does:
  Enter skips, nobody is asked when no one is at the keyboard, and `--no-record` turns it off.
- **Two more questions go with the price and the seller**, both skippable: whether it was the
  same flights, and whether the seller's payment page showed that price. A price recorded against
  a fare that was not confirmed is stored as such.
- **The window gets a "Copy the address" button** beside the Momondo one, because its deal buttons
  did not work in the owner's Firefox.

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

## Phase 2, smarter search and measurement: merged (branch `phase-2-search`, pull request #2)

Gate: the search that costs 455 calls today (91-day window, trips of 5 to 9 nights) dry-runs at
25 or fewer by default and about 40 with `--thorough`, and the bake-off report runs on the
owner's route file.

Where the gate stands:

- **Met:** the 455-pair search dry-runs at exactly 25, and at 40 with `--thorough`. `tests/test_window.py`
  and `tests/test_commands.py` pin both.
- **Met:** the owner ran the bake-off on the six routes in `docs/routes.txt` on 2026-10-08, and
  `bakeoff --report` prints it. What it found is under "Bake-off results" below.

Credits spent while building this phase: two, on the owner's say-so, for the two explore replies
in `tests/fixtures`. Monthly searches left went from 168 to 166. The owner's bake-off then made
135 live searches, inside its budget of 150; `provider_calls` in `history.db` counts them.

- [x] Departure window on `Request`; planner strategies reported through `Plan`
- [x] Return flights and booking options (seller) through the token chain
- [x] `gl` and `bags`; ranking by total cost with the catch stated
- [x] `explore` command on `google_travel_explore` (one named place is tested on real replies;
      a region or anywhere is built from the documentation; see "Not done")
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
- [x] Decide `sort_by` from that count. Decided by the owner on 2026-10-09: it stays. The
      cheapest listed fare had return flights on all 6 bake-off routes, and the closer look
      covers the rare miss.
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

- **The staged search and the closer look have not run against the live service.** Return
  flights, booking options and the countries are tested on the recorded replies. `gl` and `bags`
  are tested only against the documentation.
- **Explore for a region or for anywhere is unverified.** That is what `explore --area` and a bare
  `explore` send. SerpApi's page documents a `destinations` list with a `start_date` per place and
  no `end_date`; the parser reads that from the documentation alone. Explore for one named place,
  which is what the `explore` strategy sends, is settled: see "Found while building".
- **The `explore` strategy has not been run end to end on a real route.** Its two explore calls for
  BOS to WAW are recorded, and a test seeds a search from them; the dated searches that follow were
  not paid for.
- **The four site links were checked once, on 2026-10-08.** Momondo, Skiplagged and Kiwi.com
  opened the right search. Skyscanner named the right cities and then showed a robot check, so
  its dates were never seen. `links.json` records this per site.
- `pip install -e .` has still not been run.

### Found while building

- **Explore gives usable dates for one named place.** The reply has `start_date`, `end_date` and
  one price per airline, as the brief said and SerpApi's page does not show. `month` steers it:
  January gave 25 Jan to 3 Feb 2027, February gave 1 Feb to 10 Feb.
- **Explore's "one week" was nine nights both times**, so `planner.EXPLORE_NIGHTS` lets a one-week
  request stand in for trips of six to nine nights.
- **Explore's cheapest price can belong to a flight nobody would take.** January's $611 was a
  37-hour trip out with one stop; the next fare, $675, took ten hours. Explore gives no flight
  numbers, times or tokens, so its prices are leads for a dated search and nothing more.
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

## Bake-off results, 2026-10-08

Six routes, each a window search with the closer look, run by the owner between 23:10 and 23:36
local time. SerpApi's fare is the confirmed one. The four site prices were read by eye from the
page each link opened and typed in, once, and all 24 were marked as the same flights. The rows are
in `bakeoff_fares` in the owner's `history.db`; `bakeoff --report` prints the summary.

| Route and dates | SerpApi | Momondo | Skyscanner | Skiplagged | Kiwi.com |
| --- | --- | --- | --- | --- | --- |
| MCO,SFB to JFK,EWR,LGA, 5 to 8 Dec 2026 | $147 | $129 | $210 | $169 | $220 |
| BOS to WAW, 24 Jan to 2 Feb 2027 | $482 | $480 | $555 | $482 | $624 |
| MCO,SFB to PVG,SHA, 12 to 26 Jan 2027 | $1,094 | $997 | $1,094 | $1,057 | $1,163 |
| MCO,SFB to DCA,IAD,BWI, 14 to 17 Nov 2026 | $44 | $43 | $137 | $137 | $167 |
| BOS to LAX,BUR,SNA,LGB,ONT, 29 Jun to 5 Jul 2027 | $347 | $369 | $395 | $412 | $472 |
| MCO,SFB to WAW, 6 to 14 Apr 2027 | $704 | $653 | $681 | $680 | $720 |

- **Momondo listed the same flights for less on 5 of the 6 routes, by $33.80 (6%) on average.**
  The five savings were $97, $51, $18, $2 and $1. On the sixth, Boston to Los Angeles, it was $22 dearer.
- **The two largest savings were the two long trips from Orlando**: $97 to Shanghai and $51 to
  Warsaw. The third long trip, Boston to Warsaw, saved $2. As a share of the fare the largest was
  Orlando to New York, 12%.
- **Two Momondo fares were followed to the seller's payment page by the owner, and both held**:
  $43 against $44 (Orlando to Washington) and $997 against $1,094 (Orlando to Shanghai).
- **Skiplagged was cheaper on 2 routes** ($37 and $24), **Skyscanner on 1** ($23), **Kiwi.com on none.**
- **Another country's price was cheaper on 0 of 3**: Poland twice, China once.
- **Two one-way tickets were cheaper on 1 of 6**: Orlando to New York, $129 against $147, with a
  different flight out.
- **The cheapest listed fare had return flights on all 6 routes**, so the search in Google's own
  order was never made.

Six routes on one evening is a small sample, and only two of the five savings were followed past
the listing. Phase 3 item 2 exists to build a larger one.

## Phase 3, the Momondo next step: built, awaiting review (branch `phase-3-momondo`)

Rescoped by the owner on 2026-10-09. The bake-off justified no new provider, so this replaces the
brief's "Phase 3, second sources" and its list (Skiplagged MCP, Kiwi MCP, Travelpayouts Data API,
`personal_only` providers). The phase is now only the three items below; the owner's rulings on
how they are built are under "Decisions the owner has made".

Gate, confirmed by the owner on 2026-10-09: the three items are done and tested, the registry
still holds SerpApi alone, the mode test passes, and the phase added no call to any network. It
stands in for the brief's gate, "the mode test passes and each provider has recorded-response
tests", because no provider is being added.

Where the gate stands: met. The suite is 399 tests, 50 of them new, and all pass.
`tests/test_next_step.py` and `NextStepWindowTest` in `tests/test_gui.py` cover the three items.
`GateTest` pins that SerpApi is the only source and that `serpapi.py` is the only module importing
anything that reaches a network. `tests/test_mode.py` passes unchanged.

Credits spent in this phase: none. No search was made and no site was opened.

- [x] "Check this fare on Momondo" is the main next step after a round trip, in the window and on
      the command line, ahead of the other site links. It says plainly that Momondo listed lower
      on 5 of 6 test routes and that the saving was larger on long trips.
- [x] The owner can record the price found there, and the seller's name, so that `history.db`
      builds up real savings data over time.
- [x] `HOW-TO-USE.txt` notes that Momondo's deal buttons did not work for the owner in Firefox and
      did in Chrome.

Not built, as ruled: a Momondo provider, any scraper, or a Kiwi, Travelpayouts or Skiplagged provider.

### Decisions made while building

For the owner to confirm or overturn:

- **Two one-way tickets that beat the round trip get no Momondo step** and keep the four links
  they had. The ruling was "every round trip, confirmed or not"; here the fare is not one ticket,
  and Momondo's page for those dates would be compared with the wrong price.
- **The step says what the link cannot carry.** Momondo's address holds the airports and dates
  only. A search for more than one adult, another cabin, a stop limit or bags adds a line naming
  what to set on the page, so that a price for one adult is not recorded against a fare for two.
- **The price is typed into a small separate window, opened by a button.** A third tab would have
  broken the test that pins the two tabs, and boxes in the main window cost height the tables need.
- **"Check and book" shares the row with the Momondo buttons, to their right**, and the window
  starts two lines taller where the screen allows. Stacked, the new row and its sentence squeezed
  the flights table to nothing at the starting size.
- **The sentence is data**: `why_first` in `links.json`, beside the address pattern, as each site's
  warning is. It names its own date and should be replaced when the measurement is repeated.
- **A typed price lives only in `site_prices`.** It writes no `price_points` row and never changes
  the headline: a fare found on Momondo does not turn `Cheapest listed` into `Cheapest`.
- **Saving twice writes two rows.** The table is append-only like the others, so a price cannot be
  corrected, only added again.
- **Two existing tests changed**, both because the ruling moves Momondo out of "Same dates on" for
  a round trip: `test_the_best_dates_come_with_a_link_to_each_other_site` in
  `tests/test_commands.py` and `test_the_best_dates_can_be_opened_on_each_other_site` in
  `tests/test_gui.py`. No other existing test was touched.

### Not done

- **Nothing reads `site_prices` back.** The rows are kept; no command reports on them yet.
- **No Momondo page was opened in this phase.** Its round-trip link was last checked on
  2026-10-08, and its one-way link never has been, which is why a one-way does not lead with it.
- **The window was looked at once, on the owner's machine**, with the test fake behind it: a
  round trip nobody confirmed, with a price saved. Other screens and scalings were not tried.

## Phase 4, watches and alerts: planned, not started (branch `phase-4-watches`)

Rescoped by the owner on 2026-10-09. This replaces the brief's "Phase 4, watches and alerts" and
its gate, "a watch runs unattended for a week in a container and sends one true alert with no
false ones".

Gate for the build: all existing tests pass, the watch logic is tested with a fake clock and fake
replies, the test alert reaches the owner's phone, and one real check runs end to end. A week of
unattended running is the owner's acceptance test after the merge, and is not part of the build.

Credits: no more than 5 searches in this phase. If more are needed, ask first.

The rescope, as the owner gave it:

- **Where it runs: on the owner's Windows PC, not in a container.** One command checks every due
  watch once and exits. Windows Task Scheduler starts it daily. The owner is given the exact
  command to schedule it; nobody else schedules it. A run missed while the PC was off happens at
  the next start.
- **Alerts go through ntfy, sent with the standard library.** ntfy's current documentation is
  checked before any code is written against it. The topic is a long random name, generated by the
  program, kept in the git-ignored `.env` and never committed: anyone who knows the topic can read
  the alerts. An alert names the route, the dates and the price, says why it fired, and opens the
  Momondo link for those dates when tapped. One command sends a test alert without spending a search.
- **Budget: at most three watches, and a normal check costs one search.** A watch the monthly
  budget cannot cover is refused, and the refusal shows the arithmetic. The owner had 31 searches
  left on 2026-10-09, so the schedule proposed has to fit until the reset.
- **The unsorted search is evaluated as the check**, as the open item under Phase 2 suggests.
- **Three rules.** Alert when a fare falls below a price the owner set, falls a set percentage
  under its own recent median, or falls below Google's typical range.
- **A round trip's return flights are confirmed before any alert about it.**
- **Never alert twice for the same price.** Alert again only on a further drop.
- **Shape: "watch this fare" on specific dates.** A window watch would cost 25 searches a check,
  so it is not built. The command line comes first; the window comes last and stays small.

Not built, as ruled: a container, Telegram or email alerts, a watch on a date window.

The owner's rulings on the plan, also 2026-10-09:

- **The check is the unsorted search for a round trip and the ordinary sorted search for a one-way.**
- **A watch follows the cheapest fare on its dates with any airline.** The alert names the flights.
- **A check that fires fetches the return flights and then the sellers.** If the fare is two
  separate tickets the alert says so, and gives the one-ticket price when there is one.
- **The recent median is the median of Google's daily lows over the last 30 days**, and the
  default drop is 15%.
- **A further drop is at least $5 and at least 2% below the lowest price already alerted.** A fare
  whose return lookup came back empty is not retried at the same price.
- **Three searches are held back for each watch.** Until the reset on 8 November: two watches at
  the shortest interval that fits the 26 with at least 2 spare. After the reset watches may use at
  most 150 searches a month, and that ceiling is a setting the owner can raise.
- **Nothing about the free plan is hard-coded.** The monthly total and the reset date are read
  from the account endpoint on every run, so the budget follows a change of plan by itself.
- **`GateTest` may allow exactly two modules to reach a network**: `serpapi.py` and `notify.py`.
- **ntfy.sh, the public server, is acceptable.** The owner runs the setup and the test alert.
- **A failed run goes to a log file and a status line**, and one alert is sent when a watch has
  missed two intervals, at most once per outage.
- **The window gets one "Watch this fare" button** and a small box. Listing and removing stay on
  the command line.
- **A watch retires itself once its departure date has passed.** `watch list` shows the last
  check, the last price, the next due date and the searches used.
- **The real check may spend up to 3 searches.**

### Checked while planning, at no cost

- **The searches reset on 2026-11-08.** SerpApi's account endpoint, which is free, reports
  `plan_renewal_date`; the code does not read it yet. On 2026-10-09 it also reported 31 of 250 left.
- **ntfy, from its documentation on 2026-10-09**: a message is a POST of JSON to the server's root
  address with `topic`, `title`, `message`, `click`, `tags` and `priority`. A topic may use
  letters, digits, `-` and `_`, up to 64 characters. ntfy.sh allows 250 messages a day and keeps a
  message for 12 hours. Nothing has been sent yet.
- **The suite is 399 tests and all pass** on `main` at 1809298.

## Phase 5, web app: not started

Gate: it installs to a phone home screen and receives a push alert.

## Phase 6, public readiness: only when asked
