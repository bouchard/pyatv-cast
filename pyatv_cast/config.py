"""Remembered settings, kept in ``~/.config/pyatv-cast/config.json``.

Only the chosen TV is stored (its name and where to find it). Pairing credentials stay in
pyatv's own storage and are never copied here.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import UserError

_LOGGER = logging.getLogger(__name__)
APP_DIR = "pyatv-cast"


def config_path() -> Path:
    """Where the config file lives (honours ``XDG_CONFIG_HOME``)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / APP_DIR / "config.json"


def load() -> Dict[str, Any]:
    """Read the config; a missing or damaged file is treated as empty."""
    try:
        data = json.loads(config_path().read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as ex:
        _LOGGER.warning("ignoring unreadable config %s: %s", config_path(), ex)
        return {}


def saved_tv() -> Optional[Dict[str, Any]]:
    """The remembered TV, if any."""
    tv = load().get("tv")
    return tv if isinstance(tv, dict) and tv.get("host") else None


def save_tv(tv: Dict[str, Any]) -> None:
    """Remember ``tv`` (a dict with at least ``name`` and ``host``)."""
    path = config_path()
    data = load()
    data["tv"] = tv
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as ex:
        _LOGGER.warning("could not remember the TV in %s: %s", path, ex)


def forget_tv() -> bool:
    """Remove the remembered TV. Returns whether there was one."""
    data = load()
    if "tv" not in data:
        return False
    del data["tv"]
    try:
        config_path().write_text(json.dumps(data, indent=2) + "\n")
    except OSError as ex:
        raise UserError(f"could not update {config_path()}: {ex.strerror or ex}.") from ex
    return True
