"""Atomic XDG-compliant JSON persistence for settings, profiles, and monitor state."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pdfl.models import AppSettings, Profile


def get_config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "pdfl"


def get_state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return base / "pdfl"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    _ensure_private_dir(lock_path.parent)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_json_safe(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return dict(default)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            json.dump(payload, tmp_file, indent=2, ensure_ascii=False)
            tmp_file.write("\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class SettingsStore:
    """Atomic persistence for `settings.json`."""

    def __init__(self, path: Path | None = None) -> None:
        self._path_override = path

    @property
    def path(self) -> Path:
        return self._path_override if self._path_override is not None else get_config_dir() / "settings.json"

    def load(self) -> AppSettings:
        lock_path = self.path.with_suffix(".lock")
        with _file_lock(lock_path):
            raw = _read_json_safe(self.path, {})
        return AppSettings.from_dict(raw)

    def save(self, settings: AppSettings) -> AppSettings:
        normalized = settings.normalized()
        lock_path = self.path.with_suffix(".lock")
        with _file_lock(lock_path):
            _atomic_write_json(self.path, normalized.to_dict())
        return normalized


class ProfileStore:
    """Atomic persistence for `profiles.json` preserving stable IDs and explicit order."""

    def __init__(self, path: Path | None = None) -> None:
        self._path_override = path

    @property
    def path(self) -> Path:
        return self._path_override if self._path_override is not None else get_config_dir() / "profiles.json"

    def _load_unlocked(self) -> tuple[list[Profile], int]:
        raw = _read_json_safe(self.path, {"version": 1, "next_id": 1, "profiles": []})
        profiles: list[Profile] = []
        max_id = 0
        for item in raw.get("profiles", []) or []:
            if not isinstance(item, dict):
                continue
            try:
                prof = Profile.from_dict(item)
            except (KeyError, TypeError, ValueError):
                continue
            profiles.append(prof)
            if prof.id > max_id:
                max_id = prof.id
        next_id = max(int(raw.get("next_id", 1)), max_id + 1)
        return profiles, next_id

    def _save_unlocked(self, profiles: list[Profile], next_id: int) -> None:
        payload = {
            "version": 1,
            "next_id": next_id,
            "profiles": [p.to_dict() for p in profiles],
        }
        _atomic_write_json(self.path, payload)

    def list_profiles(self) -> list[Profile]:
        with _file_lock(self.path.with_suffix(".lock")):
            profiles, _ = self._load_unlocked()
            return profiles

    def get_profile(self, profile_id: int) -> Profile | None:
        for prof in self.list_profiles():
            if prof.id == profile_id:
                return prof
        return None

    def find_by_name_or_id(self, identifier: str) -> Profile | None:
        profiles = self.list_profiles()
        if identifier.isdigit():
            pid = int(identifier)
            for p in profiles:
                if p.id == pid:
                    return p
        low = identifier.strip().lower()
        for p in profiles:
            if p.name.strip().lower() == low:
                return p
        return None

    def add_or_update(self, profile: Profile) -> Profile:
        clean_name = profile.name.strip()
        if not clean_name:
            raise ValueError("Profile name must not be empty.")
        with _file_lock(self.path.with_suffix(".lock")):
            profiles, next_id = self._load_unlocked()
            if profile.id > 0:
                for idx, existing in enumerate(profiles):
                    if existing.id == profile.id:
                        updated = Profile(
                            id=profile.id,
                            name=clean_name,
                            monitor_settings=list(profile.monitor_settings),
                        )
                        profiles[idx] = updated
                        self._save_unlocked(profiles, next_id)
                        return updated
            assigned_id = next_id
            next_id += 1
            created = Profile(
                id=assigned_id,
                name=clean_name,
                monitor_settings=list(profile.monitor_settings),
            )
            profiles.append(created)
            self._save_unlocked(profiles, next_id)
            return created

    def delete(self, profile_id: int) -> bool:
        with _file_lock(self.path.with_suffix(".lock")):
            profiles, next_id = self._load_unlocked()
            filtered = [p for p in profiles if p.id != profile_id]
            if len(filtered) == len(profiles):
                return False
            self._save_unlocked(filtered, next_id)
            return True

    def reorder(self, profile_id: int, delta: int) -> list[Profile]:
        """Move profile up (delta < 0) or down (delta > 0) while keeping IDs intact."""
        with _file_lock(self.path.with_suffix(".lock")):
            profiles, next_id = self._load_unlocked()
            idx = next((i for i, p in enumerate(profiles) if p.id == profile_id), -1)
            if idx < 0:
                return profiles
            target_idx = max(0, min(len(profiles) - 1, idx + delta))
            if target_idx != idx:
                item = profiles.pop(idx)
                profiles.insert(target_idx, item)
                self._save_unlocked(profiles, next_id)
            return profiles


class MonitorStateStore:
    """Atomic persistence for last known monitor parameters (`monitor_state.json`)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path_override = path

    @property
    def path(self) -> Path:
        return (
            self._path_override
            if self._path_override is not None
            else get_state_dir() / "monitor_state.json"
        )

    def load_all(self) -> dict[str, dict[str, int]]:
        with _file_lock(self.path.with_suffix(".lock")):
            raw = _read_json_safe(self.path, {"version": 1, "monitors": {}})
        monitors_raw = raw.get("monitors", {}) or {}
        cleaned: dict[str, dict[str, int]] = {}
        for mid, vals in monitors_raw.items():
            if isinstance(vals, dict):
                cleaned[str(mid)] = {
                    str(k): int(v) for k, v in vals.items() if isinstance(v, (int, float))
                }
        return cleaned

    def update_monitor(self, monitor_id: str, values: dict[str, int | None]) -> None:
        with _file_lock(self.path.with_suffix(".lock")):
            raw = _read_json_safe(self.path, {"version": 1, "monitors": {}})
            monitors = raw.get("monitors")
            if not isinstance(monitors, dict):
                monitors = {}
            entry = dict(monitors.get(monitor_id, {}) or {})
            for k, v in values.items():
                if v is not None:
                    entry[k] = int(v)
            monitors[monitor_id] = entry
            _atomic_write_json(self.path, {"version": 1, "monitors": monitors})
