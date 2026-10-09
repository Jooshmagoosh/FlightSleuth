"""The one place the rest of the code gets its price sources from."""
from __future__ import annotations

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


def load(name: str) -> Provider:
    try:
        provider_class = _classes[name]
    except KeyError:
        raise UserError(f"there is no price source called {name!r}") from None
    return provider_class()


def offering(capability: Capability) -> list[Provider]:
    """A fresh instance of every source that has `capability`, in registration order."""
    return [load(name) for name, provider_class in _classes.items() if capability in provider_class.capabilities]
