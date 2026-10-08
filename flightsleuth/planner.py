"""Turn one flexible request into the concrete searches worth paying for."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from .models import Query


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
