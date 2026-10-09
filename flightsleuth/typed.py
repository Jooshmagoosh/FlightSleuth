"""A price read on another site by eye and typed in: asking for it, and keeping it in `history.db`."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from . import config, render
from .history import History


def price(text: str) -> float | None:
    """The amount somebody typed, such as "$1,234.50". None if they typed nothing; ValueError if it is no price."""
    text = text.strip()
    if not text:
        return None
    amount = float(text.replace(",", "").lstrip("$"))
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError(text)
    return amount


def yes_no(answer: str) -> bool | None:
    """True for y, False for n, None for anything else: an empty answer means "not known"."""
    return {"y": True, "n": False}.get(answer.strip().lower()[:1])


def ask_price(ask, say, prompt: str) -> float | None:
    """Ask until the answer is a price or nothing. `ask(prompt)` returns what was typed; `say(line)` shows a line."""
    while True:
        answer = ask(prompt).strip()
        try:
            return price(answer)
        except ValueError:
            say(f"{answer!r} is not a price. Type a number such as 412, or press Enter to skip.")


def record(
    step: render.NextStep, amount: float, *, seller: str = "", same_flights: bool | None = None, held: bool | None = None
) -> str:
    """Keep a price found on the step's site, beside the fare it was checked against. Returns what to say about it."""
    seller = seller.strip()
    now = datetime.now(timezone.utc)
    history = History(config.history_path())
    try:
        history.add_site_price(
            step.trip,
            step.site,
            amount,
            seller=seller,
            same_flights=same_flights,
            held=held,
            ours=step.fare,
            confirmed=step.confirmed,
            recorded_at=now,
        )
    finally:
        history.close()
    return render.recorded(step, amount, seller, now)


def ask_and_record(step: render.NextStep, ask, say) -> None:
    """Ask somebody at the keyboard what the step's site showed, and keep it. No price skips the rest."""
    amount = ask_price(ask, say, f"{step.trip.currency} price you found on {step.site} for this fare (Enter to skip): ")
    if amount is None:
        return
    seller = ask("Who sells it at that price (Enter to skip): ")
    same = yes_no(ask("Is that the same flights? y, n, or Enter if unsure: "))
    held = yes_no(ask("Did the seller's payment page show that price? y, n, or Enter if you did not get that far: "))
    say(record(step, amount, seller=seller, same_flights=same, held=held))
