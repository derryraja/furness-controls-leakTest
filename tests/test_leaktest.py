import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from leaktest import fbus
from leaktest.config import Config
from leaktest.excel_log import log_result, read_recent
from leaktest.fbus import ACK, ENQ, EOT, ETX, FBusInstrument
from leaktest.runner import format_test_ref, run_leak_test
from leaktest.simulator import SimulatedInstrument


# ── framing ──────────────────────────────────────────────────────
def test_command_frame():
    frame = fbus.build_command("01", "G000")
    assert frame[0] == EOT and frame[-2] == ETX
    assert frame[1:-2] == b"01G000"
    assert frame[-1] == fbus.calc_bcc(frame[1:-1])


def test_enquiry_frame():
    frame = fbus.build_enquiry("01", "M000")
    assert frame[-2] == ENQ
    assert frame[-1] == fbus.calc_bcc(b"01M000" + bytes([ENQ]))


def test_parse_tags_keeps_first_occurrence():
    tags = fbus.parse_tags('a187b"SN-1"e0f49.12h0.0123e5f10.0')
    assert tags == {"a": "187", "b": "SN-1", "e": "0", "f": "49.12", "h": "0.0123"}


def test_format_test_ref():
    assert format_test_ref("187") == "#000187"
    assert format_test_ref(None) == "#UNKNOWN"


# ── fake serial port for the real driver ─────────────────────────
class FakeSerial:
    """Minimal instrument that walks through a test after G000."""

    def __init__(self):
        self.out = b""
        self.polls = 0
        self.started = False

    def reset_input_buffer(self):
        self.out = b""

    @property
    def in_waiting(self):
        return len(self.out)

    def read(self, n=1):
        data, self.out = self.out[:n], self.out[n:]
        return data

    def write(self, frame):
        cmd = frame[3:7].decode()
        kind = frame[7]
        if kind == ETX:                      # command
            if cmd == "G000":
                self.started = True
            self.out = bytes([ACK])
            return
        if cmd == "M000":
            self.polls += 1
            if not self.started:
                body = "f0d0k0g0.02"
            elif self.polls < 4:
                body = "f1d0k8g50.00"
            else:
                body = "f0d1k0g0.00"
        elif cmd == "L002":
            body = 'a42b""e0f49.40h0.0200'
        else:
            body = "a42"
        self.out = body.encode() + bytes([ETX, 0])

    def close(self):
        pass


def test_fbus_driver_full_sequence(monkeypatch):
    monkeypatch.setattr(fbus.time, "sleep", lambda s: None)
    inst = FBusInstrument("TEST", log=lambda m: None)
    inst._ser = FakeSerial()
    cfg = Config(test_duration_s=30, pass_threshold_mbar_s=0.03, poll_interval_s=0.001)
    r = run_leak_test(inst, "UNIT-1", "op", cfg, log=lambda m: None)
    assert r.test_reference == "#000042"
    assert r.result == "PASS"
    assert r.leak_rate == 0.02
    assert r.pressure_start == 50.0 and r.pressure_end == 49.4


def test_own_threshold_overrides_instrument_pass(monkeypatch):
    monkeypatch.setattr(fbus.time, "sleep", lambda s: None)
    inst = FBusInstrument("TEST", log=lambda m: None)
    inst._ser = FakeSerial()
    cfg = Config(pass_threshold_mbar_s=0.01, poll_interval_s=0.001)
    assert run_leak_test(inst, "UNIT-2", "op", cfg, log=lambda m: None).result == "FAIL"


# ── simulator + Excel ────────────────────────────────────────────
def test_simulated_run_and_excel(tmp_path):
    cfg = Config(test_duration_s=10, poll_interval_s=0.5, simulate=True,
                 excel_file=str(tmp_path / "out.xlsx"))
    results = []
    for i in range(3):
        sim = SimulatedInstrument(cfg.target_pressure_mbar, cfg.test_duration_s,
                                  cfg.pass_threshold_mbar_s, speed=200, log=lambda m: None)
        r = run_leak_test(sim, f"SIM-{i}", "tester", cfg, log=lambda m: None)
        assert r.result in ("PASS", "FAIL")
        assert r.test_reference.startswith("#")
        results.append(log_result(cfg.excel_file, r))
    assert results == [1, 2, 3]
    rows = read_recent(cfg.excel_file)
    assert [row[2] for row in rows] == ["SIM-2", "SIM-1", "SIM-0"]


def test_config_roundtrip(tmp_path):
    p = str(tmp_path / "c.json")
    cfg = Config(serial_port="COM9", fbus_id="2")
    cfg._coerce()
    cfg.save(p)
    loaded = Config.load(p)
    assert loaded.serial_port == "COM9" and loaded.fbus_id == "02"
