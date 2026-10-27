"""
Simulated instrument with the same interface as ``FBusInstrument``.

Lets you try the app, demo it, or develop the UI without hardware.
Time is accelerated by ``speed`` (10 = a 30 s test takes 3 s).
"""

from __future__ import annotations

import random
import time
from typing import Callable

from .fbus import RawResult, Status

_counter = random.randint(100, 900)


class SimulatedInstrument:
    def __init__(self, target_pressure: float = 50.0, test_duration: float = 30.0,
                 threshold: float = 0.10, speed: float = 10.0,
                 log: Callable[[str], None] = print):
        self.target = target_pressure
        self.duration = test_duration
        self.threshold = threshold
        self.time_scale = max(speed, 0.1)
        self.log = log
        self._t0 = None
        self._leak = 0.0
        self._result_ready = False
        self._end_pressure = 0.0

    # Phase lengths in *simulated* seconds
    FILL_S = 5.0
    STAB_S = 3.0
    VENT_S = 2.0

    def open(self) -> None:
        self.log("Simulator connected (no hardware)")

    def close(self) -> None:
        pass

    @property
    def description(self) -> str:
        return f"Simulator ({self.time_scale:g}x speed)"

    def _sim_elapsed(self) -> float:
        return (time.time() - self._t0) * self.time_scale

    def _state(self):
        """Return (stage_code, pressure) at the current simulated time."""
        if self._t0 is None:
            return "0", round(random.uniform(-0.05, 0.05), 3)
        t = self._sim_elapsed()
        fill_end = self.FILL_S
        stab_end = fill_end + self.STAB_S
        test_end = stab_end + self.duration
        vent_end = test_end + self.VENT_S
        noise = random.uniform(-0.003, 0.003)
        if t < fill_end:
            return "6", round(self.target * t / fill_end, 3)
        if t < stab_end:
            return "7", round(self.target + 0.2 * (1 - (t - fill_end) / self.STAB_S) + noise, 3)
        if t < test_end:
            return "8", round(self.target - self._leak * (t - stab_end) + noise, 3)
        if t < vent_end:
            self._end_pressure = self._end_pressure or round(self.target - self._leak * self.duration, 3)
            return "9", round(self._end_pressure * (1 - (t - test_end) / self.VENT_S), 3)
        self._result_ready = True
        return "0", 0.0

    def read_status(self) -> Status:
        stage, pressure = self._state()
        ready = self._result_ready
        return Status(
            status_code="0" if stage == "0" else "1",
            stage_code=stage,
            pressure=pressure,
            result_ready=ready,
        )

    def start_test(self) -> bool:
        # Leak rate spread around the threshold so both outcomes appear
        self._leak = random.uniform(0.1, 1.9) * self.threshold
        self._t0 = time.time()
        self._result_ready = False
        self._end_pressure = 0.0
        return True

    def read_result(self) -> RawResult:
        global _counter
        _counter += 1
        status = "0" if self._leak <= self.threshold else "10"
        return RawResult(
            counter=str(_counter),
            status_code=status,
            pressure=self._end_pressure or None,
            leak_rate=round(self._leak, 6),
            raw_tags={"a": str(_counter), "e": status, "h": f"{self._leak:.6f}"},
        )

    def reset(self) -> bool:
        self._t0 = None
        self._result_ready = False
        return True
