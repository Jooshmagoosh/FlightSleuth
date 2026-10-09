"""Turn one flexible request into the concrete searches worth paying for."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from .models import Query

Pair = tuple[date, date | None]  # (depart, return); no return date means one way

# Trip lengths, in nights, that each explore duration is a fair stand-in for. "week" reaches nine
# because both recorded replies to a one-week request suggested a nine-night trip.
EXPLORE_NIGHTS = {"weekend": (2, 3), "week": (6, 9), "two weeks": (13, 15)}
EXPLORE_MONTHS = 6  # the explore engine only knows this month and the five after it


def date_pairs(
    depart: date,
    *,
    return_date: date | None = None,
    nights: tuple[int, int] | None = None,
    flex: int = 0,
    today: date | None = None,
) -> list[tuple[date, date | None]]:
    """Every (depart, return) pair to price.

    `flex` slides the departure either side of `depart`. The trip keeps its
    length unless `nights` gives a range of lengths to try instead.
    """
    today = today or date.today()
    departures = [depart + timedelta(days=offset) for offset in range(-flex, flex + 1)]
    departures = [d for d in departures if d >= today]
    if nights is not None:
        lengths = range(nights[0], nights[1] + 1)
    elif return_date is not None:
        lengths = [(return_date - depart).days]
    else:
        return [(d, None) for d in departures]
    return [(d, d + timedelta(days=n)) for d in departures for n in lengths]


def one_way_legs(trip: Query) -> tuple[Query, Query]:
    """The two one-way searches that cover the same dates as a round trip."""
    out = replace(trip, return_date=None)
    back = replace(
        trip,
        origin=trip.destination,
        destination=trip.origin,
        depart_date=trip.return_date,
        return_date=None,
    )
    return out, back


def window_pairs(earliest: date, latest: date, nights: tuple[int, int] | None) -> list[Pair]:
    """Every pair that departs inside the window, for every trip length asked for."""
    days = [earliest + timedelta(days=offset) for offset in range((latest - earliest).days + 1)]
    if nights is None:
        return [(d, None) for d in days]
    return [(d, d + timedelta(days=n)) for d in days for n in range(nights[0], nights[1] + 1)]


def sample(earliest: date, latest: date, nights: tuple[int, int] | None, count: int) -> list[Pair]:
    """`count` pairs spread across the window, giving every weekday and every trip length a turn.

    Fares follow the day of the week more than anything else, so the window is cut into `count`
    equal stretches and each stretch contributes the day that falls on the next weekday in
    turn. Trip lengths rotate alongside, shifted once a week so no weekday is stuck with one length.
    """
    days = [earliest + timedelta(days=offset) for offset in range((latest - earliest).days + 1)]
    lengths = list(range(nights[0], nights[1] + 1)) if nights else [None]
    count = min(count, len(days) * len(lengths))
    chosen: list[Pair] = []
    taken: set[Pair] = set()
    sweep = 0
    while len(chosen) < count:
        stretches = min(len(days), count - len(chosen))
        for index in range(stretches):
            first = index * len(days) // stretches
            stretch = days[first : max((index + 1) * len(days) // stretches, first + 1)]
            wanted = (3 * index + sweep) % 7  # steps of three reach all seven weekdays
            day = next((d for d in stretch if d.weekday() == wanted), stretch[len(stretch) // 2])
            turn = index + index // 7 + sweep
            # A later sweep can land on a day already used: take the next length it has not had.
            for shift in range(len(lengths)):
                length = lengths[(turn + shift) % len(lengths)]
                pair = (day, day + timedelta(days=length) if length is not None else None)
                if pair not in taken:
                    taken.add(pair)
                    chosen.append(pair)
                    break
        sweep += 1
        if sweep > len(days) * len(lengths):  # every pair is taken; cannot happen while count is capped
            break
    return sorted(chosen)


def neighbours(pair: Pair, earliest: date, latest: date, nights: tuple[int, int] | None) -> list[Pair]:
    """The pairs a day away from `pair` that still fit the request, most promising first.

    Sliding the whole trip comes first, because it keeps the length that just priced well;
    then a day more or less at the far end, then at the near end.
    """
    depart, back = pair
    one = timedelta(days=1)
    if back is None:
        moves = [(depart - one, None), (depart + one, None)]
    else:
        moves = [
            (depart - one, back - one),
            (depart + one, back + one),
            (depart, back - one),
            (depart, back + one),
            (depart - one, back),
            (depart + one, back),
        ]
    return [
        (d, r)
        for d, r in moves
        if earliest <= d <= latest and (r is None or nights is None or nights[0] <= (r - d).days <= nights[1])
    ]


def explore_duration(nights: tuple[int, int] | None) -> str | None:
    """The explore trip length that stands in for this range of nights, if one is close enough."""
    if nights is None:
        return None
    for name, (low, high) in EXPLORE_NIGHTS.items():
        if nights[0] <= high and nights[1] >= low:
            return name
    return None


def window_months(earliest: date, latest: date, today: date) -> list[int] | None:
    """The calendar months the window touches, or None if any is beyond what explore covers."""
    months = []
    year, month = earliest.year, earliest.month
    while (year, month) <= (latest.year, latest.month):
        if (year - today.year) * 12 + month - today.month >= EXPLORE_MONTHS:
            return None
        months.append(month)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def fit(start: date, end: date | None, earliest: date, latest: date, nights: tuple[int, int] | None) -> Pair | None:
    """A suggested pair, pulled to the nearest trip length the request allows; None if it departs outside the window."""
    if not earliest <= start <= latest:
        return None
    if nights is None:
        return (start, None)
    wanted = (end - start).days if end else (nights[0] + nights[1]) // 2
    return (start, start + timedelta(days=min(max(wanted, nights[0]), nights[1])))
