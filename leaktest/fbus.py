"""
F-BUS protocol driver.

F-BUS is the binary serial protocol used by Furness Controls leak testers
(e.g. the FCO780) over RS-232 or a USB virtual COM port.

Frame formats used here
-----------------------
Command  : EOT id1 id2 C1 C2 C3 C4 ETX BCC   -> instrument replies ACK / NAK
Enquiry  : EOT id1 id2 C1 C2 C3 C4 ENQ BCC   -> instrument replies D1..Dn ETX BCC
BCC      : XOR of every byte after EOT, up to and including ETX / ENQ

Commands / enquiries used
-------------------------
M000  status enquiry   f=status, d=new-result flag, k=stage, g=pressure
G000  start test
L002  last result      global block (a=test counter, b=serial, e=status)
                       followed by step blocks (f=pressure, h=leak reading)
E000  counters         a=total tests
H000  reset / stop

Check your instrument's F-BUS manual: tag meanings can differ between
firmware versions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

EOT = 0x04
ETX = 0x03
ENQ = 0x05
ACK = 0x06
NAK = 0x15

STAGE_NAMES = {
    "0": "Standby",
    "4": "Prefilling",
    "5": "Testing",
    "6": "Filling",
    "7": "Stabilising",
    "8": "Testing",
    "9": "Venting",
    "14": "Awaiting start",
}
TESTING_STAGES = {"5", "8"}


# ══════════════════════════════════════════════════════════════════
#  Framing helpers (pure functions -- unit tested)
# ══════════════════════════════════════════════════════════════════

def calc_bcc(data: bytes) -> int:
    """XOR checksum of all bytes."""
    bcc = 0
    for b in data:
        bcc ^= b
    return bcc


def build_command(fbus_id: str, command: str) -> bytes:
    payload = (fbus_id + command).encode("ascii") + bytes([ETX])
    return bytes([EOT]) + payload + bytes([calc_bcc(payload)])


def build_enquiry(fbus_id: str, command: str) -> bytes:
    payload = (fbus_id + command).encode("ascii") + bytes([ENQ])
    return bytes([EOT]) + payload + bytes([calc_bcc(payload)])


def parse_tags(data: str) -> dict:
    """
    Parse F-BUS tag/value pairs.

    Tags are single characters followed by either a quoted string
    (``b"ABC"``) or a number (``g99.87``).

    L002 returns the global result block first, followed by step blocks
    that reuse some tags, so the FIRST occurrence of each tag is kept.
    """
    tags: dict[str, str] = {}
    i = 0
    while i < len(data):
        tag = data[i]
        i += 1
        if i >= len(data):
            break
        if data[i] == '"':
            end = data.find('"', i + 1)
            if end == -1:
                break
            tags.setdefault(tag, data[i + 1:end])
            i = end + 1
        else:
            start = i
            while i < len(data) and (data[i].isdigit() or data[i] in ".+-"):
                i += 1
            tags.setdefault(tag, data[start:i])
    return tags


def to_float(value, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════
#  Instrument interface
# ══════════════════════════════════════════════════════════════════

@dataclass
class Status:
    status_code: str          # tag f of M000 ("0" = ready)
    stage_code: str           # tag k of M000
    pressure: Optional[float] # tag g of M000 (mbar)
    result_ready: bool        # tag d of M000 == "1"

    @property
    def stage_name(self) -> str:
        return STAGE_NAMES.get(self.stage_code, f"Stage {self.stage_code}")

    @property
    def is_testing(self) -> bool:
        return self.stage_code in TESTING_STAGES


@dataclass
class RawResult:
    counter: Optional[str]          # instrument test counter
    status_code: str                # "0" = pass
    pressure: Optional[float]       # end pressure from the result block
    leak_rate: Optional[float]      # direct leak reading, if provided
    raw_tags: dict


class FBusInstrument:
    """Real instrument over a serial / USB virtual COM port."""

    time_scale = 1.0

    def __init__(self, port: str, baud_rate: int = 9600, fbus_id: str = "01",
                 log: Callable[[str], None] = print):
        self.port = port
        self.baud_rate = baud_rate
        self.fbus_id = fbus_id
        self.log = log
        self._ser = None

    # ── connection ───────────────────────────────────────────────
    def open(self) -> None:
        import serial  # imported lazily so the simulator works without pyserial
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baud_rate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=2,
        )
        self.log(f"Connected on {self.port} @ {self.baud_rate} baud (F-BUS id {self.fbus_id})")

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    @property
    def description(self) -> str:
        return f"{self.port} @ {self.baud_rate}"

    # ── low level ────────────────────────────────────────────────
    def send_command(self, command: str) -> bool:
        ser = self._ser
        ser.reset_input_buffer()
        ser.write(build_command(self.fbus_id, command))
        time.sleep(0.2)
        resp = ser.read(1)
        if resp and resp[0] == ACK:
            return True
        what = "NAK" if resp and resp[0] == NAK else repr(resp)
        self.log(f"Command {command} not acknowledged ({what})")
        return False

    def send_enquiry(self, command: str, timeout: float = 3.0) -> str:
        ser = self._ser
        ser.reset_input_buffer()
        ser.write(build_enquiry(self.fbus_id, command))
        time.sleep(0.3)

        raw = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw += ser.read(ser.in_waiting or 1)
            if ETX in raw:
                break
            time.sleep(0.05)

        etx_pos = raw.find(bytes([ETX]))
        if etx_pos >= 0:
            raw = raw[:etx_pos]
        return raw.decode("ascii", errors="ignore")

    # ── high level (shared with the simulator) ───────────────────
    def read_status(self) -> Status:
        t = parse_tags(self.send_enquiry("M000"))
        return Status(
            status_code=t.get("f", "?"),
            stage_code=t.get("k", "?"),
            pressure=to_float(t.get("g")),
            result_ready=t.get("d") == "1",
        )

    def start_test(self) -> bool:
        return self.send_command("G000")

    def read_result(self) -> RawResult:
        t = parse_tags(self.send_enquiry("L002"))
        counter = t.get("a")
        if counter is None:
            counter = parse_tags(self.send_enquiry("E000")).get("a")
        return RawResult(
            counter=counter,
            status_code=t.get("e", "10"),
            pressure=to_float(t.get("f")),
            leak_rate=to_float(t.get("h")),
            raw_tags=t,
        )

    def reset(self) -> bool:
        return self.send_command("H000")
