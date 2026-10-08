"""Command line: plan the searches, stay inside the quota, print the cheapest."""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date

from . import config, scan
from .cache import Cache
from .errors import UserError
from .render import render_text, summarise
from .serpapi import CABINS, STOPS, SerpApi


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flightsleuth", description="Find the cheapest fares for a route by scanning dates and airports."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="find the cheapest fares for a route")
    search.add_argument("origin", type=_airports, help="departure airport(s), e.g. JFK or JFK,EWR,LGA")
    search.add_argument("destination", type=_airports, help="arrival airport(s), e.g. LHR or LHR,LGW")
    search.add_argument("--depart", type=_date, required=True, metavar="YYYY-MM-DD")
    trip = search.add_mutually_exclusive_group(required=True)
    trip.add_argument("--return", dest="return_date", type=_date, metavar="YYYY-MM-DD")
    trip.add_argument("--nights", type=_nights, metavar="N[-M]", help="trip length, or a range of lengths to try")
    trip.add_argument("--one-way", action="store_true")
    search.add_argument(
        "--flex", type=_count, default=0, metavar="DAYS", help="also try departing this many days either side"
    )
    search.add_argument("--split", action="store_true", help="also price each round trip as two one-way tickets")
    search.add_argument("--stops", choices=list(STOPS), default="any", help="most stops allowed (default: any)")
    search.add_argument("--cabin", choices=list(CABINS), default="economy")
    search.add_argument("--adults", type=_count, default=1)
    search.add_argument("--currency", default="USD")
    search.add_argument(
        "--deep", action="store_true", help="slower search that matches the Google Flights site more closely"
    )
    search.add_argument("--top", type=_count, default=10, metavar="N", help="itineraries to list (default: 10)")
    search.add_argument(
        "--max-searches",
        type=_count,
        default=scan.DEFAULT_MAX_SEARCHES,
        metavar="N",
        help=f"refuse a scan needing more live searches than this (default: {scan.DEFAULT_MAX_SEARCHES})",
    )
    search.add_argument(
        "--max-age",
        type=float,
        default=scan.DEFAULT_MAX_AGE_HOURS,
        metavar="HOURS",
        help=f"reuse saved results newer than this (default: {scan.DEFAULT_MAX_AGE_HOURS})",
    )
    search.add_argument("--fresh", action="store_true", help="ignore saved results and search again")
    search.add_argument("--dry-run", action="store_true", help="show what the scan would cost, then stop")

    commands.add_parser("gui", help="open the search window")
    commands.add_parser("quota", help="show how many SerpApi searches are left")
    cache = commands.add_parser("cache", help="show or clear saved results")
    cache.add_argument("--clear", action="store_true")
    return parser


def _searches(count: int, kind: str = "") -> str:
    noun = "search" if count == 1 else "searches"
    return f"{count} {kind} {noun}" if kind else f"{count} {noun}"


def cmd_search(args: argparse.Namespace) -> int:
    if args.split and args.one_way:
        raise UserError("--split only applies to round trips")
    request = scan.Request(
        origin=args.origin,
        destination=args.destination,
        depart=args.depart,
        return_date=args.return_date,
        nights=args.nights,
        flex=args.flex,
        split=args.split,
        stops=args.stops,
        cabin=args.cabin,
        adults=args.adults,
        currency=args.currency.upper(),
        deep=args.deep,
    )
    plan = scan.plan(request, max_age_hours=args.max_age, fresh=args.fresh)
    print(f"{_searches(plan.total)} needed: {plan.saved} saved, {plan.live} live.", file=sys.stderr)
    if plan.live > args.max_searches:
        raise UserError(
            f"that is over the limit of {args.max_searches} live searches per run. "
            f"Narrow --flex or --nights, or raise --max-searches."
        )
    if args.dry_run:
        return 0

    dots = []

    def dot(done: int, live: int) -> None:
        print("." if dots else "Searching .", end="", file=sys.stderr, flush=True)
        dots.append(done)

    try:
        outcome = scan.run(plan, on_search=dot)
    finally:
        if dots:
            print(file=sys.stderr)

    print(render_text(summarise(outcome.trips, outcome.results, split=outcome.split, top=args.top)))
    if outcome.searches_left is not None:
        print(f"\nAbout {outcome.searches_left} SerpApi searches left this month.")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import main as open_window  # imported late so the command line works without tkinter

    open_window()
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    account = SerpApi(config.load_api_key()).account()
    print(f"Plan: {account.get('plan_name')}")
    print(f"This month: {account.get('total_searches_left')} of {account.get('searches_per_month')} searches left")
    print(
        f"This hour: {account.get('this_hour_searches')} used of "
        f"{account.get('account_rate_limit_per_hour')} allowed"
    )
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    cache = Cache(config.cache_path())
    try:
        if args.clear:
            print(f"Removed {_searches(cache.clear(), 'saved')}.")
        else:
            print(f"{_searches(cache.count(), 'saved')} in {cache.path}")
    finally:
        cache.close()
    return 0


COMMANDS = {"search": cmd_search, "gui": cmd_gui, "quota": cmd_quota, "cache": cmd_cache}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except UserError as e:
        print(f"flightsleuth: {e}", file=sys.stderr)
        return 1
