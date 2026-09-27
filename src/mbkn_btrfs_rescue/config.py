"""Configuration loading (TOML) with sensible XDG fallbacks."""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_ENV = "MBKN_BTRFS_RESCUE_CONFIG"
CONFIG_NAME = "mbkn-btrfs-rescue.toml"
APP = "mbkn-btrfs-rescue"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def _path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


@dataclass
class Config:
    device: str | None = None
    tmp_dir: Path = field(default_factory=lambda: Path.home() / ".tmp" / APP)
    cache_dir: Path = field(default_factory=lambda: _xdg("XDG_CACHE_HOME", ".cache") / APP)
    db_name: str = "index.sqlite"
    exclude: list[str] = field(default_factory=lambda: [".venv", ".venv-tools"])
    source: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.cache_dir / self.db_name

    def prepare_dirs(self) -> None:
        """Create tmp/cache dirs and route Python's tempfile module to tmp_dir."""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(self.tmp_dir)


def candidate_paths(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    paths = []
    if env := os.environ.get(CONFIG_ENV):
        paths.append(Path(env))
    paths.append(Path.cwd() / CONFIG_NAME)
    # The project checkout this package lives in (src/mbkn_btrfs_rescue/config.py -> repo root).
    paths.append(Path(__file__).resolve().parents[2] / CONFIG_NAME)
    paths.append(_xdg("XDG_CONFIG_HOME", ".config") / APP / "config.toml")
    return paths


def load_config(explicit: str | None = None) -> Config:
    cfg = Config()
    for path in candidate_paths(explicit):
        if not path.is_file():
            if explicit:
                raise FileNotFoundError(f"config file not found: {path}")
            continue
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        if "device" in data:
            cfg.device = str(_path(data["device"]))
        if "tmp_dir" in data:
            cfg.tmp_dir = _path(data["tmp_dir"])
        if "cache_dir" in data:
            cfg.cache_dir = _path(data["cache_dir"])
        if "db_name" in data:
            cfg.db_name = data["db_name"]
        if "exclude" in data:
            cfg.exclude = list(data["exclude"])
        cfg.source = path
        break
    return cfg
