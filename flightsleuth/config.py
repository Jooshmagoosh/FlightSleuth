"""Where flightsleuth keeps its files, and how it finds the API key."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import UserError

KEY_VAR = "SERPAPI_API_KEY"
HOME_VAR = "FLIGHTSLEUTH_HOME"
MODE_VAR = "FLIGHTSLEUTH_MODE"
MODES = ("personal", "hosted")
TOPIC_VAR = "FLIGHTSLEUTH_NTFY_TOPIC"
NTFY_VAR = "FLIGHTSLEUTH_NTFY_SERVER"
NTFY_SERVER = "https://ntfy.sh"
WATCH_MONTHLY_VAR = "FLIGHTSLEUTH_WATCH_MONTHLY"
WATCH_MONTHLY = 150


def mode() -> str:
    """`personal` on the user's own machine, which is the default, or `hosted` when serving other people."""
    value = os.environ.get(MODE_VAR, "").strip().lower() or MODES[0]
    if value not in MODES:
        raise UserError(f"{MODE_VAR} must be personal or hosted, not {value!r}")
    return value


def home() -> Path:
    custom = os.environ.get(HOME_VAR)
    if custom:
        return Path(custom)
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / ".cache") / "flightsleuth"


def cache_path() -> Path:
    """Saved replies. Disposable: `cache --clear` empties it."""
    return home() / "cache.db"


def history_path() -> Path:
    """Price points and the call log. Kept: nothing in flightsleuth deletes from it."""
    return home() / "history.db"


def log_path() -> Path:
    """What each unattended run of the watches did, a line at a time. Kept: nothing trims it."""
    return home() / "watch.log"


def key_file() -> Path:
    """The .env that holds the key: one in the current folder if there is one, else flightsleuth's own."""
    here = Path.cwd() / ".env"
    return here if here.is_file() else home() / ".env"


def setting(name: str) -> str | None:
    """One named value from the environment, or from the .env file. None if it is missing or blank."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = key_file()
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            found, _, value = line.partition("=")
            value = value.strip().strip("\"'")
            if found.strip() == name and value:
                return value
    return None


def save_setting(name: str, value: str) -> Path:
    """Write one named value to the .env file, leaving any other lines in it alone."""
    path = key_file()
    kept = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        kept = [line for line in lines if line.partition("=")[0].strip() != name]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*kept, f"{name}={value.strip()}"]) + "\n", encoding="utf-8")
    return path


def find_api_key() -> str | None:
    """The key from the environment, or from the .env file."""
    return setting(KEY_VAR)


def load_api_key() -> str:
    key = find_api_key()
    if key is None:
        raise UserError(f"no API key found. Set {KEY_VAR}, or put {KEY_VAR}=... in a .env file in this folder.")
    return key


def save_api_key(key: str) -> Path:
    """Write the key to the .env file, leaving any other lines in it alone."""
    return save_setting(KEY_VAR, key)


def ntfy_topic() -> str | None:
    """Where alerts are sent. A secret, like the key: whoever knows the topic can read every alert."""
    topic = setting(TOPIC_VAR)
    if topic is not None and not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic):
        # The value itself is left out of the message, as a key would be.
        raise UserError(f"{TOPIC_VAR} may hold only letters, digits, - and _, and at most 64 of them")
    return topic


def ntfy_server() -> str:
    server = (setting(NTFY_VAR) or NTFY_SERVER).rstrip("/")
    if not server.startswith(("https://", "http://")):
        raise UserError(f"{NTFY_VAR} must be a web address such as {NTFY_SERVER}")
    return server


def watch_monthly() -> int:
    """The most searches the watches may use in one month of the plan, whatever the plan allows."""
    value = setting(WATCH_MONTHLY_VAR)
    if value is None:
        return WATCH_MONTHLY
    if not value.isdigit():
        raise UserError(f"{WATCH_MONTHLY_VAR} must be a whole number of searches, not {value!r}")
    return int(value)
