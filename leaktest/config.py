"""
Configuration handling.

Settings live in a local ``config.json`` next to the app (git-ignored), so
site-specific values -- COM port, test limits, output path -- never need to
be committed. ``config.example.json`` documents every key.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
)


@dataclass
class Config:
    # Connection
    serial_port: str = "COM1"           # USB virtual COM port of the instrument
    baud_rate: int = 9600
    fbus_id: str = "01"                 # F-BUS instrument address (2 chars)

    # Test parameters -- set these to match YOUR product spec and the
    # test programme configured on the instrument.
    test_duration_s: float = 30.0       # must match the instrument's test time
    target_pressure_mbar: float = 50.0  # used by the simulator / display only
    pass_threshold_mbar_s: float = 0.10 # leak-rate limit applied by this app

    # Polling
    poll_interval_s: float = 1.0
    ready_timeout_s: float = 60.0
    max_wait_s: float = 300.0

    # Output
    excel_file: str = "leak_test_results.xlsx"

    # Behaviour
    simulate: bool = False              # use the built-in simulator
    sim_speed: float = 10.0             # simulator time acceleration
    last_operator: str = ""

    # ── persistence ─────────────────────────────────────────────
    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "Config":
        cfg = cls()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            known = {f.name: f.type for f in fields(cls)}
            for key, value in data.items():
                if key in known:
                    setattr(cfg, key, value)
        cfg._coerce()
        cfg._path = path
        return cfg

    def save(self, path: str | None = None) -> None:
        path = path or getattr(self, "_path", DEFAULT_CONFIG_PATH)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)

    def _coerce(self) -> None:
        """Make sure numeric fields really are numbers after JSON / UI input."""
        for f in fields(self):
            value = getattr(self, f.name)
            if f.type in ("float", float):
                setattr(self, f.name, float(value))
            elif f.type in ("int", int):
                setattr(self, f.name, int(value))
            elif f.type in ("bool", bool):
                setattr(self, f.name, bool(value))
            else:
                setattr(self, f.name, str(value))
        self.fbus_id = self.fbus_id.zfill(2)[:2]

    def validate(self) -> list[str]:
        errors = []
        if self.test_duration_s <= 0:
            errors.append("Test duration must be greater than 0.")
        if self.pass_threshold_mbar_s <= 0:
            errors.append("Pass threshold must be greater than 0.")
        if self.poll_interval_s <= 0:
            errors.append("Poll interval must be greater than 0.")
        if not self.fbus_id.isalnum() or len(self.fbus_id) != 2:
            errors.append("F-BUS ID must be 2 characters, e.g. 01.")
        if not self.excel_file.strip():
            errors.append("Excel file path cannot be empty.")
        if not self.simulate and not self.serial_port.strip():
            errors.append("Serial port cannot be empty.")
        return errors
