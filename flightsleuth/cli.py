"""Command line: plan the searches, stay inside the quota, print the cheapest."""
from __future__ import annotations

import argparse
import re
import sys
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

from . import bakeoff, config, notify, providers, scan, typed, watch
from .cache import Cache
from .errors import UserError
from .models import Explore
from .render import NO_FARES, budget_lines, plural, render_places, render_text, summarise, watch_lines, watch_title
from .serpapi import CABINS, DURATIONS, STOPS

# The only regions SerpApi's documentation gives an id for. Any other is passed as its id.
AREAS = {"europe": "/m/02j9z", "france": "/m/0f8l9c"}
LENGTHS = {name.replace(" ", "-"): name for name in DURATIONS}
# What a plan's first calls are for, by strategy, and what the ones kept back for later are for.
FIRST = {
    "grid": "to price the dates",
    "sampled": "to sample the window",
    "one-way": "for one-way prices",
    "explore": "to ask for likely dates",
}
LATER = {
    "sampled": "to refine around the best dates",
    "one-way": "to price the best pairs as round trips",
    "explore": "to price the dates suggested",
}


def _airports(value: str) -> str:
    try:
        return scan.airports(value)
    except UserError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a date like 2026-12-10") from None


def _nights(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", value)
    if not match:
        raise argparse.ArgumentTypeError(f"{value!r} is not a trip length like 7 or 5-8")
    low = int(match[1])
    high = int(match[2] or low)
    if high < low:
        raise argparse.ArgumentTypeError(f"{value!r} runs backwards; write it as {high}-{low}")
    return low, high


def _count(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number of zero or more")
    return number


def _price(value: str) -> float:
    try:
        amount = typed.price(value)
    except ValueError:
        amount = None
    if amount is None:
        raise argparse.ArgumentTypeError(f"{value!r} is not a price like 450")
    return amount


def _country(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z]{2}", value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a two-letter country code like us or gb")
    return value.lower()


def _area(value: str) -> str:
    area = AREAS.get(value.lower(), value)
    if not re.fullmatch(r"/[mg]/\w+", area):
        names = ", ".join(AREAS)
        raise argparse.ArgumentTypeError(f"{value!r} is not a region: use {names}, or a Google id like /m/02j9z")
    return area


def _saving_options(parser: argparse.ArgumentParser) -> None:
    """The options every command that can spend searches shares."""
    parser.add_argument(
        "--max-age",
        type=float,
        default=scan.DEFAULT_MAX_AGE_HOURS,
        metavar="HOURS",
        help=f"reuse saved results newer than this (default: {scan.DEFAULT_MAX_AGE_HOURS})",
    )
    parser.add_argument("--fresh", action="store_true", help="ignore saved results and search again")
    parser.add_argument("--dry-run", action="store_true", help="show what it would cost, then stop")


def _route(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("origin", type=_airports, help="departure airport(s), e.g. JFK or JFK,EWR,LGA")
    parser.add_argument("destination", type=_airports, help="arrival airport(s), e.g. LHR or LHR,LGW")


def _travellers(parser: argparse.ArgumentParser, *, priced: bool = True) -> None:
    """Who flies and how, for every command that searches. `priced` adds bags and the country priced for."""
    parser.add_argument("--stops", choices=list(STOPS), default="any", help="most stops allowed (default: any)")
    parser.add_argument("--cabin", choices=list(CABINS), default="economy")
    parser.add_argument("--adults", type=_count, default=1)
    parser.add_argument("--currency", default="USD")
    if priced:
        parser.add_argument("--bags", type=_count, default=0, metavar="N", help="carry-on bags every price must include")
        parser.add_argument(
            "--market", type=_country, default=None, metavar="CC", help="price it as sold in this country, e.g. gb"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flightsleuth", description="Find the cheapest fares for a route by scanning dates and airports."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    _search_parser(commands.add_parser("search", help="find the cheapest fares for a route"))
    _explore_parser(commands.add_parser("explore", help="find cheap places to go when the destination is open"))
    _bakeoff_parser(
        commands.add_parser("bakeoff", help="measure each price source against SerpApi on a file of routes")
    )
    _watch_parser(commands.add_parser("watch", help="keep an eye on a fare and be told on your phone when it drops"))
    commands.add_parser("gui", help="open the search window")
    commands.add_parser("quota", help="show how many SerpApi searches are left")
    cache = commands.add_parser("cache", help="show or clear saved results")
    cache.add_argument("--clear", action="store_true")
    return parser


def _search_parser(search: argparse.ArgumentParser) -> None:
    _route(search)
    search.add_argument("--depart", type=_date, metavar="YYYY-MM-DD", help="the day to leave")
    search.add_argument(
        "--window",
        type=_date,
        nargs=2,
        metavar=("FIRST", "LAST"),
        help="leave on any day from FIRST to LAST, instead of --depart; use with --nights or --one-way",
    )
    trip = search.add_mutually_exclusive_group(required=True)
    trip.add_argument("--return", dest="return_date", type=_date, metavar="YYYY-MM-DD")
    trip.add_argument("--nights", type=_nights, metavar="N[-M]", help="trip length, or a range of lengths to try")
    trip.add_argument("--one-way", action="store_true")
    search.add_argument(
        "--flex", type=_count, default=0, metavar="DAYS", help="also try departing this many days either side"
    )
    search.add_argument("--split", action="store_true", help="also price each round trip as two one-way tickets")
    _travellers(search)
    search.add_argument(
        "--deep", action="store_true", help="slower search that matches the Google Flights site more closely"
    )
    search.add_argument(
        "--check",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="look closer at the best fare: its return flights, its sellers, another country's price and two "
        "one-ways (up to 6 more searches). On for --window, off otherwise",
    )
    search.add_argument(
        "--other-market",
        type=_country,
        default=None,
        metavar="CC",
        help="the second country --check prices the best fare in (default: the destination's)",
    )
    search.add_argument(
        "--thorough",
        action="store_true",
        help=f"price more dates and look closer at the two best fares; raises the limit to "
        f"{scan.THOROUGH_MAX_SEARCHES}",
    )
    search.add_argument(
        "--strategy",
        choices=scan.STRATEGIES,
        default="auto",
        help="how a --window is covered. auto prices every date if that fits the limit and samples the "
        "window otherwise; one-way adds up one-way prices; explore asks Google for likely dates first",
    )
    search.add_argument(
        "--risky", action="store_true", help="also list hidden-city and self-transfer fares, labelled as such"
    )
    search.add_argument("--top", type=_count, default=10, metavar="N", help="itineraries to list (default: 10)")
    search.add_argument(
        "--no-record",
        action="store_true",
        help="after a round trip, do not ask what price the site to check first showed",
    )
    search.add_argument(
        "--max-searches",
        type=_count,
        default=None,
        metavar="N",
        help=f"refuse a scan needing more live searches than this (default: {scan.DEFAULT_MAX_SEARCHES})",
    )
    _saving_options(search)


def _explore_parser(explore: argparse.ArgumentParser) -> None:
    explore.add_argument("origin", type=_airports, help="departure airport(s), e.g. MCO or MCO,SFB")
    where = explore.add_mutually_exclusive_group()
    where.add_argument(
        "--to", dest="destination", type=_airports, default=None, help="one airport, to ask when it is cheapest"
    )
    where.add_argument("--area", type=_area, default=None, help=f"a region: {', '.join(AREAS)}, or a Google id")
    explore.add_argument(
        "--month", type=_count, default=0, metavar="N", help="1 to 12 (default: any time in the next six months)"
    )
    explore.add_argument("--length", choices=list(LENGTHS), default="week", help="trip length (default: week)")
    _travellers(explore)
    explore.add_argument("--top", type=_count, default=20, metavar="N", help="places to list (default: 20)")
    _saving_options(explore)


def _bakeoff_parser(measure: argparse.ArgumentParser) -> None:
    measure.add_argument("routes", type=Path, nargs="?", help="the route file, e.g. docs/routes.txt")
    which = measure.add_mutually_exclusive_group()
    which.add_argument(
        "--routes",
        dest="count",
        type=_count,
        default=bakeoff.DEFAULT_ROUTES,
        metavar="N",
        help=f"use the first N routes in the file; 0 for all (default: {bakeoff.DEFAULT_ROUTES})",
    )
    which.add_argument(
        "--only",
        type=_count,
        default=None,
        metavar="N",
        help="run just the Nth route in the file, counting from 1, to try one before paying for them all",
    )
    measure.add_argument(
        "--budget",
        type=_count,
        default=bakeoff.DEFAULT_BUDGET,
        metavar="N",
        help=f"the most live searches the whole bake-off may plan for (default: {bakeoff.DEFAULT_BUDGET})",
    )
    measure.add_argument("--yes", action="store_true", help="spend the searches without asking first")
    measure.add_argument("--no-manual", action="store_true", help="skip the sites whose prices have to be typed in")
    measure.add_argument("--no-open", action="store_true", help="print each site's link without opening the browser")
    measure.add_argument("--report", action="store_true", help="print the report from what is already recorded; free")
    _saving_options(measure)


def _watch_parser(watching: argparse.ArgumentParser) -> None:
    actions = watching.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="watch the cheapest fare on fixed dates; adding costs nothing")
    _route(add)
    add.add_argument("--depart", type=_date, required=True, metavar="YYYY-MM-DD", help="the day to leave")
    back = add.add_mutually_exclusive_group(required=True)
    back.add_argument("--return", dest="return_date", type=_date, metavar="YYYY-MM-DD")
    back.add_argument("--nights", type=_count, metavar="N", help="come back this many nights later")
    back.add_argument("--one-way", action="store_true")
    add.add_argument("--below", type=_price, default=None, metavar="PRICE", help="alert when the fare is below this")
    add.add_argument(
        "--drop",
        type=float,
        default=watch.DEFAULT_DROP,
        metavar="PERCENT",
        help=f"alert when it is this far under the median of Google's daily lows for the last {watch.MEDIAN_DAYS} "
        f"days (default: {watch.DEFAULT_DROP:g}; 0 turns this off)",
    )
    add.add_argument(
        "--no-typical", action="store_true", help="do not alert just because it is below Google's typical range"
    )
    add.add_argument("--every", type=_count, default=1, metavar="DAYS", help="days between checks (default: 1)")
    _travellers(add, priced=False)
    add.add_argument("--dry-run", action="store_true", help="show whether the searches cover it, and add nothing")
    shown = actions.add_parser(
        "list", help="show each watch: its last check, last price, next due date and searches used"
    )
    shown.add_argument("--all", action="store_true", help="also show watches that were removed or have retired")
    remove = actions.add_parser("remove", help="stop checking a watch; its history is kept")
    remove.add_argument("number", type=_count, help="the watch's number, as `watch list` shows it")
    run = actions.add_parser("run", help="check every watch that is due, once, and exit. This spends searches")
    run.add_argument("--dry-run", action="store_true", help="show what is due and what it could cost, then stop")
    run.add_argument(
        "--max-searches", type=_count, default=None, metavar="N", help="the most live searches this run may make"
    )
    setup = actions.add_parser("setup", help="make the secret ntfy topic the alerts go to, and show how to subscribe")
    setup.add_argument("--new", action="store_true", help="replace the topic with a new one")
    actions.add_parser("test-alert", help="send a test alert to your phone; makes no search")
    limit = actions.add_parser("limit", help="show or set the most searches the watches may use in a month")
    limit.add_argument("searches", type=_count, nargs="?", help="the new limit")


def plan_line(plan: scan.Plan) -> str:
    """What a plan costs. A plan with later stages can only give a ceiling, and says what each stage is for."""
    if not plan.later:
        return f"{plural(plan.total, 'search')} needed: {plan.saved} saved, {plan.live} live."
    stages = [f"{len(plan.calls)} {FIRST[plan.strategy]}"]
    if plan.refine_calls:
        stages.append(f"up to {plan.refine_calls} {LATER[plan.strategy]}")
    if plan.check_calls:
        fares = "the best fare" if plan.checks == 1 else f"the {plan.checks} best fares"
        stages.append(f"up to {plan.check_calls} to look closer at {fares}")
    return f"Up to {plural(plan.total, 'search')} needed: {', '.join(stages)}. {plan.saved} saved, up to {plan.live} live."


def _at_keyboard() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _answer(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:  # the keyboard went away: skip, as an empty answer does
        return ""


def _progress():
    """A callback that prints a dot per live search, and a function that ends the line."""
    dots = []

    def dot(done: int, live: int) -> None:
        print("." if dots else "Searching .", end="", file=sys.stderr, flush=True)
        dots.append(done)

    def finish() -> None:
        if dots:
            print(file=sys.stderr)

    return dot, finish


def cmd_search(args: argparse.Namespace) -> int:
    if args.split and args.one_way:
        raise UserError("--split only applies to round trips")
    if (args.depart is None) == (args.window is None):
        raise UserError("give either --depart or --window")
    request = scan.Request(
        origin=args.origin,
        destination=args.destination,
        depart=args.depart or args.window[0],
        return_date=args.return_date,
        nights=args.nights,
        flex=args.flex,
        split=args.split,
        stops=args.stops,
        cabin=args.cabin,
        adults=args.adults,
        currency=args.currency.upper(),
        deep=args.deep,
        window=tuple(args.window) if args.window else None,
        strategy=args.strategy,
        thorough=args.thorough,
        check=args.check,
        market=args.market or "",
        other_market=args.other_market or "",
        bags=args.bags,
        risky=args.risky,
    )
    plan = scan.plan(request, max_age_hours=args.max_age, fresh=args.fresh, max_searches=args.max_searches)
    print(plan_line(plan), file=sys.stderr)
    try:
        scan.check_cap(plan, args.max_searches)
    except scan.OverCap as e:
        raise UserError(
            f"that is over the limit of {e.limit} live searches per run. "
            f"Narrow --flex, --nights or --window, or raise --max-searches."
        ) from None
    if args.dry_run:
        return 0

    dot, finish = _progress()
    try:
        outcome = scan.run(plan, max_searches=args.max_searches, on_search=dot)
    finally:
        finish()

    step = None
    if not outcome.trips:
        print(NO_FARES)
    else:
        summary = summarise(
            outcome.trips, outcome.results, split=outcome.split, top=args.top, checks=outcome.checks, notes=outcome.notes
        )
        print(render_text(summary, hint="Add --check to look them up, for up to 6 more searches."))
        step = summary.next_step
    if outcome.searches_left is not None:
        print(f"\nAbout {outcome.searches_left} SerpApi searches left this month.")
    if step and not args.no_record and _at_keyboard():
        print()
        typed.ask_and_record(step, _answer, print)
    return 0


def cmd_explore(args: argparse.Namespace) -> int:
    trip = Explore(
        origin=args.origin,
        destination=args.destination or "",
        area=args.area or "",
        month=args.month,
        duration=LENGTHS[args.length],
        adults=args.adults,
        cabin=args.cabin,
        stops=args.stops,
        currency=args.currency.upper(),
        market=args.market or "",
        bags=args.bags,
    )
    if "," in trip.destination:
        raise UserError("--to takes one airport; the explore engine asks about one place at a time")
    plan = scan.plan_explore(trip, max_age_hours=args.max_age, fresh=args.fresh)
    print(plan_line(plan), file=sys.stderr)
    if args.dry_run:
        return 0
    dot, finish = _progress()
    try:
        outcome = scan.run(plan, on_search=dot)
    finally:
        finish()
    where = trip.destination or next((name.title() for name, area in AREAS.items() if area == trip.area), trip.area)
    when = date(2000, trip.month, 1).strftime("%B") if trip.month else "any time in the next six months"
    length = "two weeks" if trip.duration == "two weeks" else f"a {trip.duration}"
    title = f"{trip.origin} -> {where or 'anywhere'}, {when}, {length}"
    print(render_places(title, outcome.destinations, trip.currency, top=args.top))
    if outcome.searches_left is not None:
        print(f"\nAbout {outcome.searches_left} SerpApi searches left this month.")
    return 0


def cmd_bakeoff(args: argparse.Namespace) -> int:
    if args.report:
        print(bakeoff.report())
        return 0
    if args.routes is None:
        raise UserError("name the route file, e.g. flightsleuth bakeoff docs/routes.txt")
    routes = bakeoff.read_routes(args.routes)
    if args.only is not None:
        if not 1 <= args.only <= len(routes):
            raise UserError(f"{args.routes.name} has {len(routes)} routes, so --only takes a number from 1 to {len(routes)}")
        routes = [routes[args.only - 1]]
    elif args.count:
        routes = routes[: args.count]
    plans = bakeoff.plan(routes, budget=args.budget, max_age_hours=args.max_age, fresh=args.fresh)
    for route, plan in plans:
        print(f"{route.label}: {plan_line(plan)}", file=sys.stderr)
    most = bakeoff.ceiling(plans)
    extra = " That includes one for each route in case its cheapest fare has no return flights." if most else ""
    count = plural(len(plans), "route")
    print(f"{count}: up to {plural(most, 'live search')} in all.{extra}", file=sys.stderr)
    if args.dry_run:
        return 0

    interactive = _at_keyboard()
    if most and not args.yes:
        if not interactive:
            raise UserError(f"add --yes to spend up to {plural(most, 'live search')}, or --dry-run to see the plan")
        if input(f"Spend up to {plural(most, 'live search')}? Type yes to go on: ").strip().lower() != "yes":
            raise UserError("nothing was spent")
    ask = _answer if interactive and not args.no_manual else None
    run = bakeoff.new_run()
    left = None
    stopped = None
    for done, (route, plan) in enumerate(plans):
        dot, finish = _progress()
        try:
            outcome = bakeoff.run_route(
                run, route, plan, on_search=dot, ask=ask, open_link=None if args.no_open else webbrowser.open
            )
        except UserError as e:
            # Most likely the hourly limit. What finished is recorded and saved, so say how to carry on.
            stopped = (
                f"stopped at {route.label}, after {done} of {len(plans)} routes: {e} "
                f"Run the same command again to carry on: finished routes replay from saved results "
                f"for {args.max_age:g} hours and cost nothing."
            )
            break
        finally:
            finish()
        if outcome.searches_left is not None:
            left = outcome.searches_left
    print()
    print(bakeoff.report())
    if stopped:
        raise UserError(stopped)
    if left is not None:
        print(f"\nAbout {left} SerpApi searches left this month.")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    return WATCH_ACTIONS[args.action](args)


def _watch_add(args: argparse.Namespace) -> int:
    if args.nights == 0:
        raise UserError("--nights must be 1 or more; for a one-way fare use --one-way")
    back = args.return_date or (args.depart + timedelta(days=args.nights) if args.nights else None)
    wanted = watch.trip(
        args.origin,
        args.destination,
        args.depart,
        back,
        adults=args.adults,
        cabin=args.cabin,
        stops=args.stops,
        currency=args.currency.upper(),
    )
    try:
        added = watch.add(
            wanted,
            below=args.below,
            drop_percent=args.drop,
            typical=not args.no_typical,
            every_days=args.every,
            dry_run=args.dry_run,
        )
    except watch.OverBudget as e:
        # The sums first, so the refusal that follows them can be checked.
        print("\n".join(budget_lines(e.budget)), file=sys.stderr)
        raise
    if added.budget:
        print("\n".join(budget_lines(added.budget)))
    if args.dry_run:
        print(f"That fits. Nothing was added: {watch_title(added.watch.trip)}.")
        return 0
    print(f"Watching, as number {added.watch.id}: {watch_title(added.watch.trip)}. Adding it used no searches.")
    print("It is checked the next time the watches are run: py -m flightsleuth watch run")
    if config.ntfy_topic() is None:
        print(f"Alerts are not set up yet, and nothing is checked until they are. Run: {notify.SETUP}")
    return 0


def _watch_list(args: argparse.Namespace) -> int:
    found = watch.listed(ended=args.all)
    if not found:
        print("There are no watches. Add one with: py -m flightsleuth watch add JFK LHR --depart 2026-12-08 --return 2026-12-15 --below 600")
        return 0
    now = datetime.now().astimezone()
    print("\n\n".join("\n".join(watch_lines(standing, now, watch.RESEND_HOURS)) for standing in found))
    print(f"\nWhat each run did is in {config.log_path()}")
    return 0


def _watch_remove(args: argparse.Namespace) -> int:
    removed = watch.remove(args.number)
    print(f"Removed watch {args.number}: {watch_title(removed.trip)}. Its history is kept.")
    return 0


def _watch_run(args: argparse.Namespace) -> int:
    now = datetime.now().astimezone()
    try:
        report = watch.run_due(now=now, max_searches=args.max_searches, dry_run=args.dry_run)
    except UserError as e:
        if not args.dry_run:
            watch.log([f"Nothing was checked: {e}"], now)
        raise
    except Exception as e:
        # Nobody is at the screen when the scheduler starts this, so the log has to say.
        watch.log([f"The run stopped unexpectedly: {e!r}"], now)
        raise
    print("\n".join(report.lines))
    if not args.dry_run:
        watch.log(report.lines, now)
    return 1 if report.failed else 0


def _watch_setup(args: argparse.Namespace) -> int:
    topic, new = watch.setup(new=args.new)
    if new:
        print(f"A new alert topic was made and saved in {config.key_file()}")
    else:
        print("The alert topic is already set up. To replace it: py -m flightsleuth watch setup --new")
    print(f"\n  {topic}\n")
    print("Keep it to yourself: anyone who knows the topic can read your alerts. It is in no file that git tracks.")
    print(f'On your phone, install the ntfy app, choose "Subscribe to topic" and enter the topic above. The server is {config.ntfy_server()}')
    if args.new:
        print("Remove the old topic from the app: nothing is sent to it any more.")
    print("Then send yourself a test, which makes no search: py -m flightsleuth watch test-alert")
    return 0


def _watch_test_alert(args: argparse.Namespace) -> int:
    sent = watch.send_test()
    print("Test alert sent. It should reach your phone within a few seconds. No search was made.")
    print(f"Tapping it opens {sent.click}" if sent.click else "It opens nothing when tapped, because there is no watch yet.")
    return 0


def _watch_limit(args: argparse.Namespace) -> int:
    if args.searches is not None:
        path = config.save_setting(config.WATCH_MONTHLY_VAR, str(args.searches))
        print(f"Saved in {path}")
    print(f"Watches may use up to {plural(config.watch_monthly(), 'search')} a month, and never more than the plan has left.")
    return 0


WATCH_ACTIONS = {
    "add": _watch_add,
    "list": _watch_list,
    "remove": _watch_remove,
    "run": _watch_run,
    "setup": _watch_setup,
    "test-alert": _watch_test_alert,
    "limit": _watch_limit,
}


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui import main as open_window  # imported late so the command line works without tkinter
    except ImportError:
        raise UserError("the window needs tkinter, which this Python was installed without") from None
    open_window()
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    for name in providers.names():
        for line in providers.load(name).quota_lines():
            print(line)
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    with Cache() as cache:
        if args.clear:
            print(f"Removed {plural(cache.clear(), 'saved search')}.")
        else:
            print(f"{plural(cache.count(), 'saved search')} in {cache.path}")
    return 0


COMMANDS = {
    "search": cmd_search,
    "explore": cmd_explore,
    "bakeoff": cmd_bakeoff,
    "watch": cmd_watch,
    "gui": cmd_gui,
    "quota": cmd_quota,
    "cache": cmd_cache,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except UserError as e:
        print(f"flightsleuth: {e}", file=sys.stderr)
        return 1
