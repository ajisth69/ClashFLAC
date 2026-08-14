"""Persistent configuration for the downloader. Manages the `config/` folder
(`config.json` defaults plus the `accounts` registry and pickled
`credentials.bin`) and resolves its location across repo-checkout and installed
layouts."""

import copy
import json
import os
from pathlib import Path


def _resolve_config_dir() -> Path:
    cwd_config = Path("config")
    if cwd_config.is_dir():
        return cwd_config
    override = os.environ.get("AMZDL_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "amzdl"
    return Path.home() / ".config" / "amzdl"


CONFIG_DIR = _resolve_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.bin"

DEFAULT_CONFIG = {
    "config": {
        "default_quality": "HD",
        "default_output": "~/Music/amzdl",
        "default_wvd_path": "",
        "default_account": "",
        "default_concurrency": 5,
        "default_metadata_concurrency": 10,
        "default_search_limit": 8,
        "use_link_hints": True,
    },
    "accounts": {},
}


def _write_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4)
        fh.write("\n")


def _write_default() -> dict:
    _write_config(DEFAULT_CONFIG)
    return copy.deepcopy(DEFAULT_CONFIG)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return _write_default()

    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warning: could not read {CONFIG_FILE} ({exc}); using defaults.")
        return copy.deepcopy(DEFAULT_CONFIG)

    merged = copy.deepcopy(DEFAULT_CONFIG)
    if isinstance(data, dict):
        cfg = data.get("config")
        if isinstance(cfg, dict):
            cfg = dict(cfg)
            if "use_type_hints" in cfg and "use_link_hints" not in cfg:
                cfg["use_link_hints"] = cfg.pop("use_type_hints")
            else:
                cfg.pop("use_type_hints", None)
            merged["config"].update(cfg)
        if isinstance(data.get("accounts"), dict):
            merged["accounts"] = data["accounts"]
    return merged


def get_settings() -> dict:
    return load_config()["config"]


def resolve_wvd_path(override: str | None = None) -> Path | None:
    if override:
        return Path(override).expanduser()
    configured = get_settings()["default_wvd_path"]
    if configured:
        return Path(configured).expanduser()
    return None


def load_accounts() -> dict:
    return load_config()["accounts"]


def save_accounts(accounts: dict) -> None:
    data = load_config()
    data["accounts"] = accounts
    _write_config(data)


def save_setting(key: str, value) -> None:
    data = load_config()
    data["config"][key] = value
    _write_config(data)
