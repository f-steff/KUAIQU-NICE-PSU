"""KUAIQU-NICE-POWER SPPS series DC power supply ASCII serial protocol codec.

Pure module with no side effects: the backend and the unit tests both import it, so
it must not open ports, print, or require hardware drivers.

Version: 1.0.0; Author: f-steff; License: MIT; Copyright (c) 2026 Flemming Steffensen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Frame layout. Every message is an ASCII line delimited by '<' (0x3C) and '>'
# (0x3E) with exactly 11 payload characters between them:
#   [0]     direction / CV-CC state indicator
#   [1]     function code (1..9)
#   [2..10] 9-character data field (value or 'OK' ack)
# The vendor requires the whole frame to be sent as one continuous stream with no
# intra-frame gap larger than ~3.5 character times, which is satisfied by writing
# the full string in a single write() call.
START = "<"
END = ">"
PAYLOAD_LENGTH = 11

# Direction / state indicator in payload[0].
# '0' is the PC-to-MCU "broadcast" address; '1' and 'C' are MCU-to-PC responses.
# For read responses the vendor overloads the indicator with the regulation state:
# '1' means CV (constant voltage), 'C' means CC (constant current).
DIR_PC = "0"
DIR_MCU_CV = "1"
DIR_MCU_CC = "C"

# Function codes from the vendor protocol document. Code 6 (read firmware version)
# is undocumented in the vendor table but confirmed on-device; code 5 appears in the
# table but is silent on the real device and appears unused.
FUNC_SET_VOLTAGE = 1
FUNC_READ_VOLTAGE = 2
FUNC_SET_CURRENT = 3
FUNC_READ_CURRENT = 4
FUNC_READ_VERSION = 6
FUNC_POWER_ON = 7
FUNC_POWER_OFF = 8
FUNC_CONNECT = 9

# The 9-character data field carries a value in milli-units (voltage in millivolts,
# current in milliamperes) as six right-aligned, zero-padded digits followed by three
# literal "000" padding characters. This matches the vendor PC software's "{:07.3f}"
# format: 4.580 V -> 4580 -> "004580" + "000" = "004580000". Resolution is therefore
# 0.001 V / 0.001 A.
VALUE_DIGITS = 6
PAD_DIGITS = 3
VALUE_WIDTH = VALUE_DIGITS + PAD_DIGITS  # 9
MAX_MILLI_VALUE = 999999  # Largest value that fits six digits (999.999).

# Reserved two-character prefix of the data field for a successful set ack, e.g.
# <11OK0000000> for set voltage and <13OK0000000> for set current.
ACK = "OK"

# Function-9 sub-command encoded in the first data character: 1 = remote control mode
# (required for set commands), 2 = back to local control mode.
CONNECT_CONNECT = "1"
CONNECT_DISCONNECT = "2"

_NUMERIC_DATA = re.compile(r"^[0-9]{9}$")
_VERSION_TEXT = re.compile(r"^V?(\d+(?:\.\d+)?)", re.IGNORECASE)


def _validate_milli_value(milli: int, label: str) -> None:
    """Reject out-of-range values before they produce an invalid frame.

    Keeping the error at the command layer avoids silently truncating or overflowing
    the fixed-width data field.
    """
    if milli != milli or milli in (float("inf"), float("-inf")):  # NaN / inf guard
        raise ValueError(f"Invalid {label} value: must be a finite number")
    if milli < 0:
        raise ValueError(f"Invalid {label} value: must not be negative")
    if milli > MAX_MILLI_VALUE:
        raise ValueError(f"Invalid {label} value: exceeds 999.999")


def volts_to_millivolts(volts: float) -> int:
    """Convert volts to the integer millivolts the device expects (nearest mV)."""
    try:
        value = float(volts)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid voltage value: must be a finite number") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("Invalid voltage value: must be a finite number")
    milli = int(round(value * 1000.0))
    _validate_milli_value(milli, "voltage")
    return milli


def amps_to_milliamps(amps: float) -> int:
    """Convert amperes to the integer milliamperes the device expects (nearest mA)."""
    try:
        value = float(amps)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid current value: must be a finite number") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("Invalid current value: must be a finite number")
    milli = int(round(value * 1000.0))
    _validate_milli_value(milli, "current")
    return milli


def millivolts_to_volts(milli) -> float | None:
    """Convert the protocol's millivolt field to volts (None passes through)."""
    if milli is None:
        return None
    return float(milli) / 1000.0


def milliamps_to_amps(milli) -> float | None:
    """Convert the protocol's milliamp field to amperes (None passes through)."""
    if milli is None:
        return None
    return float(milli) / 1000.0


def build_frame(direction: str, func: int | str, data: str) -> str:
    """Assemble a complete frame from direction, function code and 9-char data field.

    The width is validated so a coding bug cannot emit a malformed frame that the
    device would silently flush.
    """
    if len(str(direction)) != 1:
        raise ValueError(f"Invalid direction indicator: {direction}")
    if len(str(data)) != VALUE_WIDTH:
        raise ValueError(f"Data field must be {VALUE_WIDTH} characters, got {len(str(data))}")
    return f"{START}{direction}{func}{data}{END}"


def value_data(milli: int) -> str:
    """Render a milli-unit value as the six-digit field plus the three pad zeros."""
    return f"{int(milli):0{VALUE_DIGITS}d}" + "0" * PAD_DIGITS


def empty_data() -> str:
    """All-zero data field used by queries and by power on/off."""
    return "0" * VALUE_WIDTH


def encode_set_voltage(volts: float) -> str:
    """Encode a set-voltage command (acknowledged with 'OK' by the device)."""
    return build_frame(DIR_PC, FUNC_SET_VOLTAGE, value_data(volts_to_millivolts(volts)))


def encode_set_current(amps: float) -> str:
    """Encode a set-current command (acknowledged with 'OK' by the device)."""
    return build_frame(DIR_PC, FUNC_SET_CURRENT, value_data(amps_to_milliamps(amps)))


def encode_read_voltage() -> str:
    """Encode the read-voltage query (device ignores the query data field)."""
    return build_frame(DIR_PC, FUNC_READ_VOLTAGE, empty_data())


def encode_read_current() -> str:
    """Encode the read-current query."""
    return build_frame(DIR_PC, FUNC_READ_CURRENT, empty_data())


def encode_power_on() -> str:
    """Encode the output-enable command (no acknowledgement)."""
    return build_frame(DIR_PC, FUNC_POWER_ON, empty_data())


def encode_power_off() -> str:
    """Encode the output-disable command (no acknowledgement)."""
    return build_frame(DIR_PC, FUNC_POWER_OFF, empty_data())


def encode_connect() -> str:
    """Encode remote-control mode, which locks the front panel."""
    return build_frame(DIR_PC, FUNC_CONNECT, CONNECT_CONNECT + "0" * (VALUE_WIDTH - 1))


def encode_disconnect() -> str:
    """Encode local-control mode, which unlocks the front panel."""
    return build_frame(DIR_PC, FUNC_CONNECT, CONNECT_DISCONNECT + "0" * (VALUE_WIDTH - 1))


def encode_read_version() -> str:
    """Encode the firmware-version query (undocumented function 6, verified on-device)."""
    return build_frame(DIR_PC, FUNC_READ_VERSION, empty_data())


@dataclass
class Frame:
    """One decoded frame. `value_milli` and `text` are mutually exclusive."""

    raw: str
    dir: str
    func: str
    data: str
    from_mcu: bool
    cv_cc: str | None
    is_ack: bool
    value_milli: int | None
    text: str | None


def decode_frame(frame) -> Frame | None:
    """Decode one complete frame string.

    Returns None for anything that does not match the 11-character payload shape so
    callers can distinguish "not a frame" from "a frame carrying zero".
    """
    if not isinstance(frame, str):
        return None
    content = frame.strip()
    # A complete frame must carry both delimiters; FrameParser guarantees this and
    # direct callers get None (not a guess) for anything else.
    if not content.startswith(START) or not content.endswith(END):
        return None
    payload = content[1:-1]
    if len(payload) != PAYLOAD_LENGTH:
        return None

    direction = payload[0]
    func = payload[1]
    data = payload[2:]
    from_mcu = direction in (DIR_MCU_CV, DIR_MCU_CC)
    cv_cc = "CC" if direction == DIR_MCU_CC else ("CV" if direction == DIR_MCU_CV else None)

    # Set-command acknowledgements carry a literal "OK" prefix instead of a number.
    if data.startswith(ACK):
        return Frame(frame, direction, func, data, from_mcu, cv_cc, True, None, None)

    # Everything else is a fixed-width integer in milli-units (the first six digits;
    # the last three are always "000" padding). Function 6 (read version) is the one
    # exception: its data field is an opaque string (e.g. "V1.300000"), so it is kept
    # as text rather than a number.
    if not _NUMERIC_DATA.match(data):
        return Frame(frame, direction, func, data, from_mcu, cv_cc, False, None, data)
    return Frame(frame, direction, func, data, from_mcu, cv_cc, False,
                 int(data[:VALUE_DIGITS], 10), None)


def parse_version_text(text) -> str | None:
    """Normalise a firmware version string ("V1.300000" -> "1.3")."""
    if not isinstance(text, str) or not text:
        return None
    match = _VERSION_TEXT.match(text)
    if not match:
        return text
    return re.sub(r"\.?0+$", "", match.group(1)) or "0"


class FrameParser:
    """Streaming parser that extracts complete '<' ... '>' frames from a byte stream.

    The backend reads arbitrary serial chunks, so frames can arrive split across
    reads or concatenated. Stray bytes outside a frame are discarded, which mirrors
    the device's own 3.5-character-time flush rule.
    """

    def __init__(self) -> None:
        self.buffer = ""

    def push(self, chunk) -> list[Frame]:
        """Append a chunk (bytes or str) and return every complete frame found."""
        if isinstance(chunk, (bytes, bytearray)):
            text = bytes(chunk).decode("latin-1")
        else:
            text = str(chunk)
        self.buffer += text

        frames: list[Frame] = []
        while True:
            start = self.buffer.find(START)
            if start < 0:
                # No start marker: nothing further in the buffer can be a frame.
                self.buffer = ""
                break
            if start > 0:
                # Drop noise before the first start marker.
                self.buffer = self.buffer[start:]
            end = self.buffer.find(END)
            if end < 0:
                # Start marker present but frame incomplete; wait for more bytes.
                break
            candidate = self.buffer[: end + 1]
            self.buffer = self.buffer[end + 1 :]
            decoded = decode_frame(candidate)
            if decoded is not None:
                frames.append(decoded)
        return frames

    def reset(self) -> None:
        """Discard buffered bytes (used between independent request/response pairs)."""
        self.buffer = ""


def iter_frames(chunks: Iterable) -> list[Frame]:
    """Convenience helper: decode every frame contained in a sequence of chunks."""
    parser = FrameParser()
    out: list[Frame] = []
    for chunk in chunks:
        out.extend(parser.push(chunk))
    return out
