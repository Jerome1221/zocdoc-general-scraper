from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workspace:
    """All mutable collector state lives under one workspace directory."""

    root: Path

    @classmethod
    def from_value(cls, value: str | Path | None = None) -> Workspace:
        raw = value or os.environ.get("ZOCDOC_WORKSPACE") or "workspace"
        return cls(Path(raw).expanduser().resolve())

    @property
    def db(self) -> Path:
        return self.root / "collector.db"

    @property
    def locations_csv(self) -> Path:
        return self.root / "zocdoc_locations.csv"

    @property
    def captured(self) -> Path:
        return self.root / "captured"

    @property
    def listing_dir(self) -> Path:
        return self.captured / "listings"

    @property
    def profile_dir(self) -> Path:
        return self.captured / "profiles"

    @property
    def rejected_listing_dir(self) -> Path:
        return self.captured / "listing_retries"

    @property
    def updater_snapshot_dir(self) -> Path:
        return self.captured / "updater_snapshots"

    @property
    def specialty_dir(self) -> Path:
        return self.captured / "specialties"

    @property
    def specialty_landing_dir(self) -> Path:
        return self.captured / "specialty_landings"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.csv"

    @property
    def blocked_flag(self) -> Path:
        return self.root / "BLOCKED.flag"

    def ensure(self) -> Workspace:
        self.root.mkdir(parents=True, exist_ok=True)
        self.listing_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.rejected_listing_dir.mkdir(parents=True, exist_ok=True)
        self.updater_snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.specialty_dir.mkdir(parents=True, exist_ok=True)
        self.specialty_landing_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self
