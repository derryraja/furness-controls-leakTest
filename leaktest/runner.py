"""
Test sequence -- hardware-agnostic. Works with ``FBusInstrument`` or
``SimulatedInstrument`` and reports progress through a callback so it can
run in a background thread behind the GUI.
"""

from __future__ import annotations

import datetime
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .config import Config


class TestAborted(Exception):
    pass


@dataclass
class Progress:
    stage: str
    pressure: Optional[float]
    elapsed_s: float
    test_fraction: Optional[float]   # 0..1 during the test stage, else None


@dataclass
class TestResult:
    device_number: str
    test_reference: str
    timestamp: datetime.datetime
    operator: str
    pressure_start: float
    pressure_end: float
    pressure_drop: float
    leak_rate: float
    threshold: float
    result: str                      # "PASS" / "FAIL"
    instrument_status: str

    @property
    def passed(self) -> bool:
        return self.result == "PASS"


def format_test_ref(counter) -> str:
    """Format the instrument's test counter like the front panel: 187 -> '#000187'."""
    try:
        return f"#{int(counter):06d}"
    except (TypeError, ValueError):
        return "#UNKNOWN"


def run_leak_test(instrument, device_number: str, operator: str, cfg: Config,
                  on_progress: Callable[[Progress], None] = lambda p: None,
                  log: Callable[[str], None] = print,
                  cancel: Optional[threading.Event] = None) -> TestResult:
    cancel = cancel or threading.Event()
    scale = getattr(instrument, "time_scale", 1.0)
    poll = cfg.poll_interval_s / scale

    def check_cancel():
        if cancel.is_set():
            try:
                instrument.reset()
            finally:
                raise TestAborted("Test aborted by operator")

    # ── 1. wait for ready ────────────────────────────────────────
    log("Waiting for instrument to be ready…")
    deadline = time.time() + cfg.ready_timeout_s
    st = instrument.read_status()
    while st.status_code != "0" and time.time() < deadline:
        check_cancel()
        on_progress(Progress(f"Not ready (status {st.status_code})", st.pressure, 0, None))
        time.sleep(1)
        st = instrument.read_status()
    if st.status_code != "0":
        log(f"Instrument not ready after {cfg.ready_timeout_s:.0f}s — proceeding anyway")
    else:
        log("Instrument ready")
    pre_start_pressure = st.pressure

    # ── 2. start ─────────────────────────────────────────────────
    check_cancel()
    log("Sending START")
    if not instrument.start_test():
        log("START not acknowledged — instrument may have started anyway")

    # ── 3. poll until complete ───────────────────────────────────
    t0 = time.time()
    test_stage_t0 = None
    p_test_first = p_test_last = None
    finished = False
    while time.time() < t0 + cfg.max_wait_s:
        check_cancel()
        elapsed = time.time() - t0
        st = instrument.read_status()

        fraction = None
        if st.is_testing:
            if test_stage_t0 is None:
                test_stage_t0 = time.time()
                p_test_first = st.pressure
            p_test_last = st.pressure if st.pressure is not None else p_test_last
            in_test = (time.time() - test_stage_t0) * scale
            fraction = min(in_test / cfg.test_duration_s, 1.0)
        elif test_stage_t0 is not None:
            fraction = 1.0

        on_progress(Progress(st.stage_name, st.pressure, elapsed * scale, fraction))

        if st.result_ready:
            log("Result available")
            finished = True
            break
        if elapsed * scale > 5 and st.status_code in ("0", "2"):
            log("Instrument returned to standby")
            finished = True
            break
        time.sleep(poll)

    if not finished:
        log(f"No result after {cfg.max_wait_s:.0f}s — reading whatever is available")

    # ── 4. read result ───────────────────────────────────────────
    raw = instrument.read_result()
    test_ref = format_test_ref(raw.counter)
    log(f"Test reference {test_ref}")

    # Start pressure: first reading in the test stage; fall back to the
    # reading taken before START if the test stage was never observed.
    p_start = p_test_first if p_test_first is not None else (pre_start_pressure or 0.0)
    # End pressure: result block value, else last reading in the test stage.
    p_end = raw.pressure if raw.pressure is not None else (p_test_last or 0.0)
    drop = p_start - p_end

    leak = raw.leak_rate if raw.leak_rate is not None else drop / cfg.test_duration_s

    result = "PASS" if raw.status_code == "0" else "FAIL"
    if leak > cfg.pass_threshold_mbar_s:      # own limit as a safety check
        result = "FAIL"

    instrument.reset()

    return TestResult(
        device_number=device_number,
        test_reference=test_ref,
        timestamp=datetime.datetime.now(),
        operator=operator or "N/A",
        pressure_start=round(p_start, 3),
        pressure_end=round(p_end, 3),
        pressure_drop=round(drop, 3),
        leak_rate=round(leak, 6),
        threshold=cfg.pass_threshold_mbar_s,
        result=result,
        instrument_status=raw.status_code,
    )
