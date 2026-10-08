"""Where flightsleuth keeps its files, and how it finds the API key."""
from __future__ import annotations

import os
from pathlib import Path

from .errors import UserError

KEY_VAR = "SERPAPI_API_KEY"
HOME_VAR = "FLIGHTSLEUTH_HOME"


def home() -> Path:
    custom = os.environ.get(HOME_VAR)
    if custom:
        return Path(custom)
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / ".cache") / "flightsleuth"


def cache_path() -> Path:
    return home() / "cache.db"


def key_file() -> Path:
    """The .env that holds the key: one in the current folder if there is one, else flightsleuth's own."""
    here = Path.cwd() / ".env"
    return here if here.is_file() else home() / ".env"


def find_api_key() -> str | None:
    """The key from the environment, or from the .env file."""
    key = os.environ.get(KEY_VAR, "").strip()
    if key:
        return key
    path = key_file()
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name, _, value = line.partition("=")
            value = value.strip().strip("\"'")
            if name.strip() == KEY_VAR and value:
                return value
    return None


def load_api_key() -> str:
    key = find_api_key()
    if key is None:
        raise UserError(f"no API key found. Set {KEY_VAR}, or put {KEY_VAR}=... in a .env file in this folder.")
    return key


def save_api_key(key: str) -> Path:
    """Write the key to the .env file, leaving any other lines in it alone."""
    path = key_file()
    kept = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        kept = [line for line in lines if line.partition("=")[0].strip() != KEY_VAR]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*kept, f"{KEY_VAR}={key.strip()}"]) + "\n", encoding="utf-8")
    return path
