"""Config load/merge for Argus.

Config lives as a TOML file under the per-user config directory (via
platformdirs). It is auto-created with defaults on first run if missing.

API keys are NEVER read from or written to the config file — they come
from the environment only (e.g. GEMINI_API_KEY / GOOGLE_API_KEY).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w
from platformdirs import user_config_dir, user_data_dir

APP_NAME = "argus"

DEFAULTS: dict[str, Any] = {
    "capture": {
        "window_interval_seconds": 5,
        "screen_interval_seconds": 300,
        "camera_interval_seconds": 300,
        "screen_enabled": True,
        "camera_enabled": True,
        "camera_device_index": 0,
        "image_webp_quality": 80,
    },
    "storage": {
        # empty string => default to the platformdirs data dir at runtime
        "data_location": "",
        "retention_days": 30,
    },
    "vision": {
        "provider": "google",
        "model": "gemini-2.5-flash-lite",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/models",
        "sampling_count": 3,
    },
    "dashboard": {
        # Bound to 127.0.0.1 only, no auth (single-user, localhost,
        # disk-encryption trust model).
        "port": 8477,
    },
    # Reports are generated on demand only (CLI: `argus report daily`).
    # No scheduler, hence no report_schedule config.
}

# Env vars checked, in order, for the vision API key. Never read from config.
API_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def config_path() -> Path:
    return Path(user_config_dir(APP_NAME)) / "config.toml"


def default_data_dir() -> Path:
    return Path(user_data_dir(APP_NAME))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=lambda: DEFAULTS)
    path: Path = field(default_factory=config_path)

    @property
    def data_dir(self) -> Path:
        loc = self.raw["storage"]["data_location"]
        return Path(loc) if loc else default_data_dir()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "argus.sqlite3"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def api_key(self) -> str | None:
        """Vision API key, sourced from the environment only. Never stored
        in or read from the config file."""
        for var in API_KEY_ENV_VARS:
            val = os.environ.get(var)
            if val:
                return val
        return None


def load_config() -> Config:
    """Load config from disk, creating it with defaults if missing.

    Values present on disk are merged over the defaults so new default keys
    introduced by future versions show up automatically.
    """
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            tomli_w.dump(DEFAULTS, f)
        return Config(raw=DEFAULTS, path=path)

    with open(path, "rb") as f:
        on_disk = tomllib.load(f)

    merged = _deep_merge(DEFAULTS, on_disk)
    return Config(raw=merged, path=path)


# Keys the settings UI is allowed to write. Anything outside this shape
# (in particular any "api_key" field, or unknown sections/keys) is dropped
# on save -- API keys are env-only, never persisted to the config file.
_EDITABLE_KEYS: dict[str, tuple[str, ...]] = {
    "capture": (
        "window_interval_seconds",
        "screen_interval_seconds",
        "camera_interval_seconds",
        "screen_enabled",
        "camera_enabled",
        "camera_device_index",
        "image_webp_quality",
    ),
    "storage": ("data_location", "retention_days"),
    "vision": ("provider", "model", "endpoint", "sampling_count"),
    "dashboard": ("port",),
}


def save_config(config: Config, updates: dict[str, dict[str, Any]]) -> Config:
    """Write ``updates`` (section -> {key: value}) over the current config
    and persist to disk as TOML.

    Only keys listed in ``_EDITABLE_KEYS`` are ever written -- this is the
    enforcement point that guarantees an api_key (or any other unknown key)
    can never be persisted to the config file, no matter what a caller
    passes in.
    """
    # Only ever persist known sections/keys (drops stray legacy sections,
    # e.g. an old removed report-schedule block, and anything unknown).
    new_raw: dict[str, Any] = {
        section: dict(config.raw.get(section, {})) for section in DEFAULTS
    }
    for section, allowed_keys in _EDITABLE_KEYS.items():
        section_updates = updates.get(section)
        if not section_updates:
            continue
        new_raw.setdefault(section, {})
        for key in allowed_keys:
            if key in section_updates:
                new_raw[section][key] = section_updates[key]

    config.path.parent.mkdir(parents=True, exist_ok=True)
    with open(config.path, "wb") as f:
        tomli_w.dump(new_raw, f)

    return Config(raw=new_raw, path=config.path)
