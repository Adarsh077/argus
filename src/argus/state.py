"""Small persistent runtime-state store for Argus.

Distinct from `config` (user-tunable TOML). This holds machine-generated
runtime state that the user should not edit, e.g. the XDG ScreenCast
`restore_token` that makes silent re-capture possible on KDE-Wayland.

Stored as JSON at <data_dir>/state.json.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("argus.state")

_lock = threading.Lock()


class State:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "state.json"

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("Could not read state file %s; treating as empty", self.path, exc_info=True)
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        with _lock:
            return self._read().get(key, default)

    def set(self, key: str, value: Any) -> None:
        with _lock:
            data = self._read()
            data[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            tmp.replace(self.path)
