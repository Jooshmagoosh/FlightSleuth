"""Links that open the same search on other sites. The address patterns are data, in links.json."""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import NamedTuple

from .models import Query


@functools.cache
def sites() -> tuple[dict, ...]:
    return tuple(json.loads(Path(__file__).with_name("links.json").read_text(encoding="utf-8"))["sites"])


def site_link(site: dict, trip: Query) -> str:
    def airports(codes: str) -> str:
        chosen = codes if site["airports"] == "all" else codes.split(",")[0]
        return chosen.lower() if site.get("lowercase") else chosen

    pattern = site["one_way" if trip.return_date is None else "round_trip"]
    return pattern.format(
        **{
            "origin": airports(trip.origin),
            "destination": airports(trip.destination),
            "depart": trip.depart_date.strftime(site["dates"]),
            "return": trip.return_date.strftime(site["dates"]) if trip.return_date else "",
        }
    )


def _label(site: dict) -> str:
    return f"{site['name']} ({site['warning']})" if site.get("warning") else site["name"]


def other_sites(trip: Query) -> list[tuple[str, str]]:
    """(label, address) for the same dates on each other site. A site's warning is part of its label."""
    return [(_label(site), site_link(site, trip)) for site in sites()]


class First(NamedTuple):
    """The site to check before the others, opened on one trip's dates."""

    name: str  # as it is recorded, such as "Momondo"
    label: str  # as it is shown: the name, with the site's warning if it has one
    url: str
    why: str  # what was measured that put it first, and when


def first_site(trip: Query) -> First | None:
    site = next((site for site in sites() if site.get("why_first")), None)
    return First(site["name"], _label(site), site_link(site, trip), site["why_first"]) if site else None
