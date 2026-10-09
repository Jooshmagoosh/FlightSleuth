"""Price sources. Code outside this package asks the registry and never builds a source itself."""
from .base import Capability, DataClass, Provider
from .registry import load, names, offering, register, unregister
from .serpapi import SerpApiProvider

register(SerpApiProvider)
