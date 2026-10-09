"""Alerts to a phone through ntfy (https://docs.ntfy.sh/publish/, checked on 2026-10-09).

A message is one POST of JSON to the server's root address. The topic travels in the body, never
in the address, so it stays out of error messages and logs: whoever knows the topic can read
every alert sent to it.
"""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import config
from .errors import UserError

USER_AGENT = "flightsleuth/0.1"
TIMEOUT_S = 30
SETUP = "py -m flightsleuth watch setup"
# Called through this name, so that the tests can stand a fake server in its place.
urlopen = urllib.request.urlopen


class NotifyError(UserError):
    pass


@dataclass(frozen=True)
class Alert:
    title: str
    message: str
    click: str = ""  # the address the phone opens when the alert is tapped


def new_topic() -> str:
    """A topic nobody could guess: 256 random bits, in the characters ntfy allows, 56 of its 64."""
    return "flightsleuth-" + secrets.token_urlsafe(32)


def send(alert: Alert) -> None:
    """Send one alert, once. A message that may have arrived is never sent again by this function."""
    topic = config.ntfy_topic()
    if topic is None:
        raise NotifyError(f"no alert topic is set up yet. Run: {SETUP}")
    body = {"topic": topic, "title": alert.title, "message": alert.message}
    if alert.click:
        body["click"] = alert.click
    request = urllib.request.Request(
        config.ntfy_server(),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_S) as response:
            response.read()
    except urllib.error.HTTPError as e:
        raise NotifyError(f"ntfy refused the alert: {_reason(e)}".replace(topic, "<topic>")) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NotifyError(f"the alert could not be sent: network error: {getattr(e, 'reason', e)}") from None


def _reason(e: urllib.error.HTTPError) -> str:
    try:
        return f"HTTP {e.code}, {json.load(e)['error']}"
    except (ValueError, KeyError, TypeError):
        return f"HTTP {e.code} {e.reason}"
