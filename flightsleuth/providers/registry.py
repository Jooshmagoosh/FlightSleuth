"""The one place the rest of the code gets its price sources from."""
from __future__ import annotations

from .. import config
from ..errors import UserError
from .base import Capability, DataClass, Provider

_classes: dict[str, type[Provider]] = {}


def register(provider_class: type[Provider]) -> None:
    """Add a source, or replace the one already registered under the same name."""
    labels = {
        "name": str,
        "capabilities": frozenset,
        "cost_per_call": (int, float),
        "data_class": DataClass,
    }
    for label, kind in labels.items():
        if not isinstance(getattr(provider_class, label, None), kind):
            raise TypeError(f"{provider_class.__name__} must set {label} before it can be registered")
    _classes[provider_class.name] = provider_class


def unregister(name: str) -> None:
    _classes.pop(name, None)


def names() -> list[str]:
    """Registered sources, in the order they were registered."""
    return list(_classes)


def _usable(provider_class: type[Provider]) -> bool:
    """A hosted product must never run a source meant for one person's own machine."""
    return not (config.mode() == "hosted" and provider_class.data_class is DataClass.PERSONAL_ONLY)


def load(name: str) -> Provider:
    try:
        provider_class = _classes[name]
    except KeyError:
        raise UserError(f"there is no price source called {name!r}") from None
    if not _usable(provider_class):
        raise UserError(f"the {name} source is for personal use only and cannot be loaded in hosted mode")
    return provider_class()


def offering(capability: Capability) -> list[Provider]:
    """A fresh instance of every usable source that has `capability`, in registration order."""
    return [
        provider_class()
        for provider_class in _classes.values()
        if capability in provider_class.capabilities and _usable(provider_class)
    ]
