"""FWSC validation, MIDI/HID framing, authentication and transfer workflow."""
from __future__ import annotations
import binascii
import ctypes
import importlib.util
import json
import os
import platform
import queue
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from .resources import (TITLE, STOCK_SHA, BRIDGE_SHA, InstallerError, Cancelled, TransportLost,
                        application_dir, data_root, log_root, entrypoint, digest, factory_bytes, native_helper_path, verify_native_files)

IDENTITY_QUERY = bytes.fromhex("F0 00 32 45 00 00 00 40 7F F7")

ENTER_OTA = bytes.fromhex("F0 22 24 35 7F F7")

HID_VID, HID_PID = 0x4D4A, 0x4155

def jl_xor(data: bytes, key: int = 0xFFFF) -> bytes:
    """JL header stream, reset at each header/record; not a signature bypass."""
    result = bytearray()
    for value in data:
        result.append(value ^ (key & 255))
        key = ((key << 1) ^ (0x1021 if key & 0x8000 else 0)) & 0xFFFF
    return bytes(result)


@dataclass(frozen=True)
class Firmware:
    file_data: bytes
    payload: bytes
    name: str
    version: int
    slots: int
    sha256: str
    sections: tuple

    @classmethod
    def parse(cls, data: bytes) -> "Firmware":
        if not 2048 <= len(data) <= 16 * 1024 * 1024:
            raise InstallerError("Invalid firmware size.")
        found = None
        # FUN_14001dbe0/FUN_14001dd20: 36-slot, then 20-slot wrapper.
        for slots in (36, 20):
            if len(data) < slots * 48:
                continue
            chars = []
            terminated = False
            valid = True
            for index in range(slots):
                value = data[index * 48 + 47]
                if terminated:
                    valid = valid and value == 0x7D
                elif value == 0x7D:
                    terminated = True
                else:
                    chars.append(chr((value - index - 1) & 255))
            match = re.fullmatch(r"BlackBox_([0-9]{3})", "".join(chars))
            if valid and terminated and match:
                payload = b"".join(data[i*48:i*48+47] for i in range(slots)) + data[slots*48:]
                found = (slots, payload, int(match.group(1)))
                break
        if found is None:
            raise InstallerError("Not a supported BlackBox FWSC file (20/36-slot wrapper).")
        slots, payload, version = found
        if version != 20:
            raise InstallerError(f"This installer preserves V20. Selected firmware advertises V{version}.")
        header = jl_xor(payload[:64])
        hcrc, table_crc, size, count, flags, table_end = struct.unpack_from("<HHIHHI", header)
        if binascii.crc_hqx(header[2:], 0) != hcrc:
            raise InstallerError("FWSC header checksum failed.")
        if size != len(payload) or not 1 <= count <= 64:
            raise InstallerError("Truncated firmware or unsupported file table.")
        if header[16:32].split(b"\0", 1)[0] != b"AC791N":
            raise InstallerError("Unexpected firmware target; expected BlackBox AC791N.")
        if 64 + count * 80 > len(payload):
            raise InstallerError("Firmware file table is out of bounds.")
        table = payload[64:64+count*80]
        if binascii.crc_hqx(table, 0) != table_crc:
            raise InstallerError("FWSC file table checksum failed.")
        sections = []
        for i in range(count):
            record = jl_xor(table[i*80:(i+1)*80])
            tag, seq, checksum, offset, length, reserved = struct.unpack_from("<HHIIII", record)
            name = record[64:80].split(b"\0", 1)[0].decode("ascii", "strict")
            if seq != i or offset < 64+count*80 or offset+reserved > len(payload) or length > reserved:
                raise InstallerError(f"Invalid file table entry: {name or i}.")
            # CRC for these transport blobs is over stored, unpadded bytes.
            # Other files use different per-file encoding; do not guess its CRC.
            if name in ("flash.bin", "ota.bin", "tail.bin"):
                if binascii.crc_hqx(payload[offset:offset+length], 0) != checksum:
                    raise InstallerError(f"Firmware checksum failed: {name}.")
            sections.append((name, offset, length, checksum))
        if not {"flash.bin", "ota.bin", "tail.bin"}.issubset({s[0] for s in sections}):
            raise InstallerError("Required BlackBox firmware components are missing.")
        if b"JLUFW" not in payload[-64:]:
            raise InstallerError("Firmware tail marker is missing.")
        return cls(data, payload, "BlackBox", version, slots, digest(data), tuple(sections))

    def slice(self, offset: int, length: int) -> bytes:
        if offset < 0 or length <= 0 or offset+length > len(self.payload):
            raise InstallerError(f"Device requested firmware outside the file: 0x{offset:X}/{length}.")
        return self.payload[offset:offset+length]


# ---------- MIDI framing: continuous little-endian 8-bit -> 7-bit stream ----------
def pack7(data: bytes) -> bytes:
    result = bytearray()
    value = bits = 0
    for byte in data:
        value |= byte << bits
        bits += 8
        while bits >= 7:
            result.append(value & 0x7F)
            value >>= 7
            bits -= 7
    if bits:
        result.append(value & 0x7F)
    return bytes(result)


def unpack7(data: bytes) -> bytes:
    result = bytearray()
    value = bits = 0
    for byte in data:
        if byte > 127:
            raise InstallerError("Invalid MIDI SysEx data.")
        value |= byte << bits
        bits += 7
        if bits >= 8:
            result.append(value & 255)
            value >>= 8
            bits -= 8
    return bytes(result)


def midi_frame(command: int, body: bytes = b"") -> bytes:
    return b"\x00\x59" + bytes([command]) + len(body).to_bytes(3, "little") + body + bytes([(~sum(body)) & 255])


def parse_midi_frame(raw: bytes) -> tuple[int, bytes]:
    if len(raw) < 7 or raw[:2] != b"\x00\x59":
        raise InstallerError("Unexpected BlackBox MIDI header.")
    size = int.from_bytes(raw[3:6], "little")
    if len(raw) != 7+size or raw[-1] != (~sum(raw[6:-1]) & 255):
        raise InstallerError("BlackBox MIDI length/checksum failed.")
    return raw[2], raw[6:-1]


def parse_identity(raw: bytes) -> tuple[str, int]:
    command, body = parse_midi_frame(raw)
    if command != 0x11:
        raise InstallerError("Expected a BlackBox identity reply.")
    text = body.split(b"\0", 1)[0].decode("ascii", "strict")
    # FUN_140018380 compares the prefix case-insensitively; the real bootloader
    # reports 'ota-BlackBox_021' (its own version, not the payload's V20).
    match = re.fullmatch(r"(?i)(BlackBox|OTA-BlackBox)_([0-9]{3})", text)
    if not match:
        raise InstallerError(f"Unsupported device identity: {text!r}.")
    name = "OTA-BlackBox" if match.group(1).casefold().startswith("ota") else "BlackBox"
    return name, int(match.group(2))


def upload_request(raw: bytes) -> tuple[int, int, int]:
    command, body = parse_midi_frame(raw)
    if command != 0x30 or len(body) != 8:
        raise InstallerError("Invalid firmware upload request.")
    return body[0], int.from_bytes(body[1:5], "little"), int.from_bytes(body[5:8], "little")


def upload_response(flash_type: int, address: int, data: bytes) -> bytes:
    body = bytes([flash_type]) + address.to_bytes(4, "little") + len(data).to_bytes(3, "little") + data
    return midi_frame(0x30, body)


# ---------- JL challenge-response implementation ----------
AUTH_KEY = bytes.fromhex("06775F87918DD423005DF1D8CF0C142B")
AUTH_MAC = bytes.fromhex("112233332211")
AUTH_BIAS = bytes.fromhex(
    "77F156247E471B86BD708E1E3B73160364AC285AC9B337C50A10B7A3BAB197463D05DC666EF69AF80D589567C6AAABECA0689B96D4EBBF434936E96A89D8C38A946399BC7BBEC122BB5C71D51F92575D8F44411D51E64017FBFD193234B8612ACA236FDA39F7A2017FD631E7DE8004DD2C5982AFA8E00FCDA1123E30D11CD03A33722E4F9002130675CE87C2EFB2AD7D3815E1529F7A6C2F27C4E281A9CF8DC0D7DFFF6076148C5E5509E408C74220FCD25091D94C629EE8B9A6F91A00210BFA359C4E4B6948CB0EC8A45BEA8407B418F4AE6BDBA7CC3F8B4A0C3C25E5544D4583ED11F0B05393F27426B59D6D7CF32DF156247E471B86BD708E1E3B731603B6AC285AC9B337C50A10B7A3BAB1974688"
)
EXP = [pow(45, i, 257) & 255 for i in range(256)]
LOG = [0] * 256
for _i, _v in enumerate(EXP):
    LOG[_v] = _i
PERM = (8, 11, 12, 15, 2, 1, 6, 5, 10, 9, 14, 13, 0, 7, 4, 3)


def key_schedule(key) -> list[int]:
    rotated = list(key) + [0]
    for v in key:
        rotated[16] ^= v
    result = list(key)
    for j in range(1, 17):
        rotated = [((v << 3) | (v >> 5)) & 255 for v in rotated]
        result.extend((rotated[(j+i) % 17] + AUTH_BIAS[j*16+15-i]) & 255 for i in range(16))
    return result


def auth_round(state, keys, n):
    state = [v ^ keys[n*32+i] if i % 4 in (0, 3) else (v+keys[n*32+i]) & 255 for i, v in enumerate(state)]
    state = [EXP[v] if i % 4 in (0, 3) else LOG[v] for i, v in enumerate(state)]
    state = [(v+keys[n*32+16+i]) & 255 if i % 4 in (0, 3) else v ^ keys[n*32+16+i] for i, v in enumerate(state)]
    for j in range(4):
        pairs = []
        for i in range(0, 16, 2):
            pairs.extend(((2*state[i]+state[i+1]) & 255, (state[i]+state[i+1]) & 255))
        state = [pairs[p] for p in PERM] if j < 3 else pairs
    return state


def whitening(state, keys):
    return [v ^ keys[256+i] if i % 4 in (0, 3) else (v+keys[256+i]) & 255 for i, v in enumerate(state)]


def auth_response(challenge: bytes) -> bytes:
    if len(challenge) != 16:
        raise InstallerError("Invalid authentication challenge size.")
    keys = key_schedule(AUTH_KEY)
    state = list(challenge)
    for n in range(8):
        state = auth_round(state, keys, n)
    state = whitening(state, keys)
    state = [((v ^ challenge[i]) + AUTH_MAC[i % 6]) & 255 for i, v in enumerate(state)]
    mod_key = []
    adds = (0xE9, 0xDF, 0xB3, 0x95)
    xors = (0xE5, 0xC1, 0xA7, 0x83)
    for i, v in enumerate(AUTH_KEY):
        if i < 8:
            mod_key.append((v+adds[i//2]) & 255 if i % 2 == 0 else v ^ xors[i//2])
        else:
            mod_key.append(v ^ adds[(i-8)//2] if i % 2 == 0 else (v+xors[(i-8)//2]) & 255)
    keys = key_schedule(mod_key)
    previous = state[:]
    for n in range(8):
        if n == 2:
            state = [v ^ previous[i] if i % 4 in (0, 3) else (v+previous[i]) & 255 for i, v in enumerate(state)]
        state = auth_round(state, keys, n)
    return bytes(whitening(state, keys))


def auth_selftest():
    if auth_response(bytes(range(16))).hex() != "e5807e887bde07630b4d0e715b3e8991":
        raise InstallerError("Authentication self-test failed. No device operation started.")


# ---------- RCSP and the 64-byte JL HID transport ----------
@dataclass(frozen=True)
class Rcsp:
    command: bool
    needs_reply: bool
    opcode: int
    sequence: int
    status: int
    data: bytes


def rcsp_command(opcode: int, sequence: int, data: bytes = b"", needs_reply=True) -> bytes:
    body = bytes([sequence]) + data
    return b"\xFE\xDC\xBA" + bytes([0xC0 if needs_reply else 0x80, opcode]) + struct.pack(">H", len(body)) + body + b"\xEF"


def rcsp_response(opcode: int, sequence: int, data: bytes = b"", status=0) -> bytes:
    body = bytes([status, sequence]) + data
    return b"\xFE\xDC\xBA\x00" + bytes([opcode]) + struct.pack(">H", len(body)) + body + b"\xEF"


def parse_rcsp(raw: bytes) -> Rcsp:
    if len(raw) < 9 or raw[:3] != b"\xFE\xDC\xBA" or raw[-1] != 0xEF:
        raise InstallerError("Malformed RCSP packet.")
    size = int.from_bytes(raw[5:7], "big")
    command = bool(raw[3] & 0x80)
    if len(raw) != size+8 or size < (1 if command else 2):
        raise InstallerError("RCSP packet length mismatch.")
    return Rcsp(command, bool(raw[3] & 0x40), raw[4], raw[7] if command else raw[8],
                0 if command else raw[7], raw[8:-1] if command else raw[9:-1])


def hid_reports(kind: int, message: int, data: bytes) -> list[bytes]:
    if len(data) > 65534:
        raise InstallerError("HID packet is too large.")
    frame = b"JL" + bytes([kind << 4, 0]) + struct.pack(">H", len(data)+1) + bytes([message]) + data + b"\xED"
    return [frame[i:i+64].ljust(64, b"\0") for i in range(0, len(frame), 64)]


class HidDecoder:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, report: bytes):
        if len(report) == 65 and report[0] == 0:
            report = report[1:]
        self.buffer.extend(report)
        result = []
        while self.buffer:
            if not any(self.buffer):
                self.buffer.clear()
                break
            if len(self.buffer) < 6:
                break
            if self.buffer[:2] != b"JL" or self.buffer[3] != 0:
                raise InstallerError("Unexpected HID framing.")
            size = int.from_bytes(self.buffer[4:6], "big")
            if size < 1 or size > 65535:
                raise InstallerError("Invalid HID message length.")
            end = 6+size
            if len(self.buffer) <= end:
                break
            if self.buffer[end] != 0xED:
                raise InstallerError("HID trailer mismatch.")
            result.append((self.buffer[2] >> 4, self.buffer[6], bytes(self.buffer[7:end])))
            # Each JL packet ends in a report padded to 64 bytes.
            padded = ((end+1+63)//64)*64
            if len(self.buffer) < padded:
                raise InstallerError("Short HID report.")
            del self.buffer[:padded]
        return result


class Control:
    def __init__(self, emit=None, log_dir=None):
        self.emit = emit or (lambda kind, value: None)
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.critical = False
        self.phase = "idle"
        self.log_dir = log_dir
        self.log_path = None
        self._log = None
        self.start = time.monotonic()

    def open_log(self, mode, firmware):
        directory = self.log_dir or log_root()
        directory.mkdir(parents=True, exist_ok=True)
        self.log_path = directory / (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3) + ".log")
        self._log = self.log_path.open("w", encoding="utf-8", buffering=1)
        self.log(f"{TITLE}; mode={mode}; platform={platform.system()} {platform.machine()}; Python={platform.python_version()}")
        self.log(f"FWSC SHA256={firmware.sha256}; wrapper={firmware.name}_{firmware.version:03d}; slots={firmware.slots}; payload={len(firmware.payload)}")

    def log(self, text):
        if self._log:
            self._log.write(f"{time.monotonic()-self.start:10.3f} {text}\n")

    def status(self, text):
        self.phase = text
        self.log(text)
        self.emit("status", text)

    def progress(self, value):
        self.emit("progress", max(0, min(100, int(value))))

    def check(self):
        if self.cancelled.is_set() and not self.critical:
            raise Cancelled("Cancelled. Firmware transfer was not started.")

    def request_cancel(self) -> bool:
        with self.lock:
            if self.critical:
                self.emit("status", "Writing firmware. Cancel is unavailable until reboot verification finishes.")
                return False
            self.cancelled.set()
            return True

    def begin_critical(self):
        with self.lock:
            self.check()
            self.critical = True
            self.emit("critical", True)

    def pause(self, seconds):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            self.check()
            time.sleep(min(0.025, max(0, deadline-time.monotonic())))

    def close(self):
        if self._log:
            self._log.close()
            self._log = None


def terminal_sysex(raw: bytes) -> bytes:
    command, body = parse_midi_frame(raw)
    if (command != 0x30 or len(body) != 16 or int.from_bytes(body[1:5], "little") not in (0xE0000000, 0xF0000000)
            or int.from_bytes(body[5:8], "little") != 8 or body[8:] != b"success\0"):
        raise InstallerError("The isolated helper accepts only the E/F completion ACK.")
    return b"\xF0"+pack7(raw)+b"\xF7"


def native_helper_environment():
    env = dict(os.environ)
    paths = [str(native_helper_path().parent), str(Path(sys.executable).parent)]
    spec = importlib.util.find_spec("PySide6")
    if spec and spec.origin:
        paths.insert(1, str(Path(spec.origin).parent))
    env["PATH"] = os.pathsep.join(paths+[env.get("PATH", "")])
    for key in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_PLATFORM", "PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    return env


def native_helper_preflight(control):
    helper = verify_native_files()
    # The exact uploaded executable dispatches this flag before QApplication.
    # Missing arguments return 64 BEFORE opening any MIDI device (FUN_140020ec0).
    result = subprocess.run([str(helper), "--midi-terminal-ack"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=native_helper_environment(), cwd=application_dir(),
                            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    control.log(f"Native helper preflight (no MIDI): exit={result.returncode}; SHA256={digest(helper.read_bytes())}")
    if result.returncode != 64:
        control.log(result.stdout.decode("utf-8", errors="replace"))
        raise InstallerError(f"The bundled M-UPGRADE helper could not load its DLLs (exit {result.returncode}). "
                             "Extract the complete ZIP and close other update applications.")
    control.native_helper = helper


def terminal_ack_child(output_name: str, packet_hex: str, api=None):
    """Output-only child on macOS/Linux; no input handle or application window."""
    message = bytes.fromhex(packet_hex)
    if message[:1] != b"\xF0" or message[-1:] != b"\xF7":
        raise InstallerError("Invalid terminal ACK SysEx.")
    if terminal_sysex(unpack7(message[1:-1])) != message:
        raise InstallerError("Invalid terminal ACK encoding.")
    if api is None:
        import rtmidi
        api = rtmidi
    options = {"rtapi": api.API_WINDOWS_MM} if sys.platform == "win32" else {}
    output = api.MidiOut(**options)
    try:
        ports = output.get_ports()
        if ports.count(output_name) != 1:
            raise InstallerError("Terminal ACK MIDI output is unavailable or ambiguous.")
        output.open_port(ports.index(output_name))
        output.send_message(list(message))
        time.sleep(0.1)
    finally:
        output.close_port()
        output.delete()
    return 0


class MidiConnection:
    def __init__(self, api, in_index, out_index, control):
        self.control = control
        self.api = api
        self.closed = False
        options = {"rtapi": api.API_WINDOWS_MM} if sys.platform == "win32" else {}
        self.input = api.MidiIn(queue_size_limit=4096, **options)
        self.output = api.MidiOut(**options)
        try:
            self.output_name = self.output.get_ports()[out_index]
            self.input.ignore_types(sysex=False, timing=True, active_sense=True)
            self.input.open_port(in_index)
            self.output.open_port(out_index)
        except Exception as exc:
            self.close()
            raise InstallerError(f"Cannot open MIDI ports. Close Altar, M-UPGRADE and the DAW. {exc}") from exc

    def close(self, strict=False):
        if self.closed:
            return
        errors = []
        for name, device in (("input", self.input), ("output", self.output)):
            try:
                device.close_port()
            except Exception as exc:
                errors.append(f"{name}: {exc}")
            try:
                device.delete()
            except Exception as exc:
                errors.append(f"{name} release: {exc}")
        self.closed = True
        if errors:
            self.control.log("MIDI close: "+"; ".join(errors))
            if strict:
                raise TransportLost("Cannot release the main MIDI handles before terminal ACK: "+"; ".join(errors))

    def send_sysex(self, message):
        self.control.check()
        try:
            self.output.send_message(list(message))
        except Exception as exc:
            raise TransportLost(f"USB-MIDI write failed: {exc}") from exc

    def send(self, raw):
        self.send_sysex(b"\xF0" + pack7(raw) + b"\xF7")

    def send_terminal(self, raw):
        """Release both ports BEFORE the E/F ACK, before the terminal ACK."""
        message = terminal_sysex(raw)
        self.control.check()
        self.close(strict=True)
        self.control.log("Main MIDI input/output closed before isolated terminal ACK.")
        if sys.platform == "win32":
            helper = getattr(self.control, "native_helper", None) or verify_native_files()
            output = self.api.MidiOut(rtapi=self.api.API_WINDOWS_MM)
            try:
                ports = output.get_ports()
            finally:
                output.delete()
            if ports.count(self.output_name) != 1:
                raise InstallerError("The selected MIDI output changed before the terminal ACK. Restart the device and reconnect.")
            command = [str(helper), "--midi-terminal-ack", str(ports.index(self.output_name)), message.hex()]
            env = native_helper_environment()
        else:
            command = [sys.executable, "-I", str(entrypoint()), "--terminal-ack", self.output_name, message.hex()]
            env = dict(os.environ)
            for key in ("PYTHONHOME", "PYTHONPATH"):
                env.pop(key, None)
        self.control.log(f"Isolated terminal ACK: output={self.output_name!r}, packet={message.hex()}, helper={Path(command[0]).name}")
        process = subprocess.Popen(command, stdout=self.control._log or subprocess.DEVNULL,
                                   stderr=subprocess.STDOUT, env=env, cwd=application_dir(),
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        deadline = time.monotonic()+10
        try:
            while process.poll() is None:
                self.control.check()
                if time.monotonic() > deadline:
                    raise InstallerError("Isolated MIDI terminal ACK timed out. No automatic retry was performed.")
                self.control.pause(0.025)
            self.control.log(f"Isolated terminal ACK helper exited with code {process.returncode}.")
            if process.returncode:
                reason = {64: "invalid arguments", 65: "invalid packet", 66: "MIDI output unavailable",
                          67: "could not open MIDI output"}.get(process.returncode, "native helper failed")
                raise InstallerError(f"Isolated MIDI terminal ACK failed: {reason} (exit {process.returncode}).")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def receive(self, timeout):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.control.check()
            try:
                item = self.input.get_message()
            except Exception as exc:
                raise TransportLost(f"USB-MIDI read failed: {exc}") from exc
            if item:
                message = bytes(item[0])
                if message[:1] == b"\xF0" and message[-1:] == b"\xF7":
                    return unpack7(message[1:-1])
            time.sleep(0.005)
        raise InstallerError("USB-MIDI reply timed out.")

    def identity(self):
        # Discard old queued messages before this read-only transaction.
        for _ in range(4096):
            if not self.input.get_message():
                break
        self.send_sysex(IDENTITY_QUERY)
        deadline = time.monotonic()+1.5
        while time.monotonic() < deadline:
            raw = self.receive(max(0.01, deadline-time.monotonic()))
            if len(raw) >= 3 and raw[:3] == b"\x00\x59\x11":
                return parse_identity(raw)
        raise InstallerError("BlackBox identity not received.")


class MidiDevices:
    def __init__(self, control, api=None):
        if api is None:
            import rtmidi
            api = rtmidi
        self.api, self.control = api, control

    def ports(self):
        options = {"rtapi": self.api.API_WINDOWS_MM} if sys.platform == "win32" else {}
        i, o = self.api.MidiIn(**options), self.api.MidiOut(**options)
        try:
            return i.get_ports(), o.get_ports()
        finally:
            i.delete()
            o.delete()

    def discover(self, selected=None, normal_only=False):
        ins, outs = self.ports()
        pairs = []
        if selected:
            # Names, not stale indices, survive USB re-enumeration.
            pairs = [(i, o) for i, n in enumerate(ins) for o, m in enumerate(outs) if (n, m) == selected]
        else:
            tokens = ("blackbox", "usb-midi", "usb midi", "sinco", "m-vave", "mvave")
            candidates_i = [i for i, n in enumerate(ins) if any(t in n.casefold() for t in tokens)]
            candidates_o = [o for o, n in enumerate(outs) if any(t in n.casefold() for t in tokens)]
            pairs = [(i, o) for i in candidates_i for o in candidates_o]
        if len(pairs) > 16:
            raise InstallerError("Several MIDI devices are present. Select the exact ports in USB connection.")
        valid = []
        errors = []
        for i, o in pairs:
            self.control.check()
            connection = None
            try:
                connection = MidiConnection(self.api, i, o, self.control)
                name, version = connection.identity()
                if normal_only and name != "BlackBox":
                    continue
                valid.append((ins[i], outs[o], name, version))
            except Cancelled:
                raise
            except Exception as exc:
                errors.append(str(exc))
            finally:
                if connection:
                    connection.close()
        if not valid:
            raise InstallerError("BlackBox not detected. Connect USB, close other MIDI apps and check USB connection." +
                                 (" " + errors[-1] if errors else ""))
        if len(valid) != 1:
            raise InstallerError("More than one BlackBox port pair replied. Select the exact ports in USB connection.")
        a, b, name, version = valid[0]
        ins, outs = self.ports()
        if a not in ins or b not in outs:
            raise InstallerError("MIDI device changed during detection. Reconnect and retry.")
        return MidiConnection(self.api, ins.index(a), outs.index(b), self.control), (name, version), (a, b)


class HidConnection:
    def __init__(self, module, path, control):
        self.control = control
        self.device = module.device()
        self.decoder = HidDecoder()
        self.pending = []
        try:
            self.device.open_path(path)
        except Exception as exc:
            self.close()
            hint = " On Linux, install the included HID permission rule." if sys.platform.startswith("linux") else ""
            raise InstallerError("Cannot open BlackBox HID loader: " + str(exc) + hint) from exc

    def close(self):
        try:
            self.device.close()
        except Exception:
            pass

    def send(self, kind, message, data=b""):
        for report in hid_reports(kind, message, data):
            self.control.check()
            try:
                count = self.device.write(b"\0"+report)
            except Exception as exc:
                raise TransportLost(f"HID write failed: {exc}") from exc
            if count not in (64, 65):
                raise TransportLost(f"Incomplete HID write ({count} bytes).")

    def receive(self, timeout):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.control.check()
            if self.pending:
                return self.pending.pop(0)
            try:
                data = bytes(self.device.read(65, max(1, min(100, int((deadline-time.monotonic())*1000)))))
            except Exception as exc:
                raise TransportLost(f"HID read failed: {exc}") from exc
            if data:
                self.pending.extend(self.decoder.feed(data))
        raise InstallerError("HID reply timed out.")

    def authenticate(self):
        self.control.status("Authenticating USB loader…")
        self.send(0, 1)
        kind, message, body = self.receive(2)
        if kind != 1:
            raise InstallerError("Unexpected HID authentication-ready response.")
        self.send(2, 0, bytes.fromhex("FEDCBAC00600020001EF"))
        self.control.pause(0.05)
        challenge = secrets.token_bytes(16)
        self.send(2, 0, b"\0"+challenge)
        kind, message, body = self.receive(2)
        if kind != 2 or body != b"\x01"+auth_response(challenge):
            raise InstallerError("Device authentication did not match.")
        self.send(2, 0, b"\x02pass")
        kind, message, body = self.receive(2)
        if kind != 2 or len(body) != 17 or body[0] != 0:
            raise InstallerError("Device authentication challenge is invalid.")
        self.send(2, 0, b"\x01"+auth_response(body[1:]))
        kind, message, body = self.receive(2)
        if kind != 2 or body != b"\x02pass":
            raise InstallerError("Device authentication was rejected.")

    def transaction(self, opcode, sequence, data=b""):
        self.send(2, 0, rcsp_command(opcode, sequence, data))
        kind, message, body = self.receive(3)
        if kind != 2:
            raise InstallerError(f"Unexpected HID response during RCSP {opcode:02X}.")
        response = parse_rcsp(body)
        if response.command or response.opcode != opcode or response.sequence != sequence or response.status != 0:
            raise InstallerError(f"RCSP {opcode:02X} rejected (opcode={response.opcode:02X}, sequence={response.sequence}, status={response.status}).")
        return response.data


class Installer:
    def __init__(self, control: Control, midi=None, hid_module=None):
        self.control = control
        self.midi = midi or MidiDevices(control)
        if hid_module is None:
            import hid
            hid_module = hid
        self.hid = hid_module
        self.wrote = False

    def hid_devices(self):
        entries = self.hid.enumerate(HID_VID, HID_PID)
        paths = {item["path"] for item in entries}
        if len(paths) > 1:
            raise InstallerError("Multiple HID loader interfaces are present. Keep only one target BlackBox connected.")
        return list(paths)

    def prepare_midi(self, connection, firmware):
        c = self.control
        c.status("Checking V20 firmware with the device…")
        connection.send_sysex(ENTER_OTA)
        c.pause(2)
        deadline = time.monotonic()+120
        while time.monotonic() < deadline:
            c.check()
            flash, address, length = upload_request(connection.receive(30))
            c.log(f"MIDI request flash={flash} address=0x{address:08X} length={length}")
            if address & 0xF0000000 == 0xC0000000:
                connection.send(upload_response(flash, address, b"success\0"))
                continue
            if address & 0xFF000000 == 0xD0000000:
                connection.send(upload_response(flash, address, b"success\0"))
                code = address & 255
                reason = {0x83: "the firmware is identical to the installed firmware",
                          0x97: "the firmware is not accepted by this device"}.get(code, "verification failed")
                raise InstallerError(f"Device rejected V20: {reason} (0x{address:08X}).")
            if address == 0xE0000000:
                connection.send_terminal(upload_response(flash, address, b"success\0"))
                c.status("Waiting for USB update mode…")
                return
            if address == 0xF0000000:
                raise InstallerError("Unexpected completion during firmware preparation.")
            if length > 32768:
                raise InstallerError("Unsupported MIDI preparation block size.")
            connection.send(upload_response(flash, address, firmware.slice(address, length)))
        raise InstallerError("Firmware preparation timed out. No HID transfer started.")

    def transfer_hid(self, connection, firmware):
        c = self.control
        connection.authenticate()
        c.status("Validating firmware with the USB loader…")
        cfg_request = connection.transaction(0xE1, 1)
        if len(cfg_request) != 6:
            raise InstallerError("USB loader returned an invalid configuration request.")
        offset, length = struct.unpack(">IH", cfg_request)
        result = connection.transaction(0xE2, 2, firmware.slice(offset, length))
        if result != b"\0":
            raise InstallerError("USB loader rejected the V20 firmware configuration: " + result.hex(" "))
        # E3 starts the writing transaction. Cancellation is locked BEFORE E3.
        c.begin_critical()
        connection.transaction(0xE3, 3)
        c.status("Installing firmware…")
        deadline = time.monotonic()+180
        ranges = []
        blocks = 0
        complete = False
        while time.monotonic() < deadline:
            kind, message, body = connection.receive(min(30, max(0.1, deadline-time.monotonic())))
            if kind != 2:
                raise InstallerError("Unexpected HID message while writing firmware.")
            request = parse_rcsp(body)
            if not request.command:
                c.log(f"HID response opcode={request.opcode:02X} status={request.status}")
                continue
            if request.opcode == 0xE5:
                if len(request.data) != 6:
                    raise InstallerError("Invalid HID firmware block request.")
                offset, length = struct.unpack(">IH", request.data)
                if length > 65524:
                    raise InstallerError("Requested HID block exceeds RCSP transport limit.")
                data = firmware.slice(offset, length)
                connection.send(2, 0, rcsp_response(0xE5, request.sequence, data))
                self.wrote = True
                blocks += 1
                c.log(f"HID block {blocks}: offset=0x{offset:X}, length={length}")
                ranges.append((offset, offset+length))
                merged = []
                for start, end in sorted(ranges):
                    if merged and start <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                    else:
                        merged.append((start, end))
                ranges = merged
                c.progress(min(95, sum(b-a for a, b in ranges)*95/len(firmware.payload)))
            elif request.opcode == 0xE8:
                if not blocks:
                    raise InstallerError("Loader finished without requesting firmware data.")
                try:
                    connection.send(2, 0, rcsp_response(0xE8, request.sequence))
                except TransportLost:
                    # E8 was received. A reboot can remove the interface during ACK.
                    # Success still requires normal V20 MIDI to return afterward.
                    c.log("E8 completion received; loader disappeared during completion ACK.")
                complete = True
                break
            else:
                c.log(f"Unhandled loader command opcode=0x{request.opcode:02X}")
                if request.needs_reply:
                    connection.send(2, 0, rcsp_response(request.opcode, request.sequence, status=1))
                raise InstallerError(f"Unsupported USB loader command: 0x{request.opcode:02X}.")
        if not complete:
            raise InstallerError("USB loader did not confirm completion.")
        c.log(f"HID transfer completion acknowledged; blocks={blocks}")
        c.progress(96)

    def transfer_midi(self, connection, firmware):
        """Legacy transport selected ONLY after an OTA-BlackBox identity."""
        c = self.control
        c.status("Installing firmware over USB-MIDI…")
        c.begin_critical()
        # FUN_140012740: with the device already in OTA mode, wait 3 s, resend
        # the OTA-enter SysEx, then wait 2 s before reading requests.
        c.pause(3)
        connection.send_sysex(ENTER_OTA)
        c.pause(2)
        deadline = time.monotonic()+240
        blocks = 0
        highest = 0
        while time.monotonic() < deadline:
            flash, address, length = upload_request(connection.receive(30))
            c.log(f"OTA-MIDI request: flash={flash}, address=0x{address:08X}, length={length}")
            if address & 0xF0000000 == 0xC0000000:
                # Normal send (FUN_14001f6e0); only E/F use the isolated ACK,
                # which closes these ports and must stay terminal.
                connection.send(upload_response(flash, address, b"success\0"))
                continue
            if address & 0xFF000000 == 0xD0000000:
                connection.send(upload_response(flash, address, b"success\0"))
                raise InstallerError(f"OTA-MIDI firmware rejected: 0x{address:08X}.")
            if address == 0xE0000000:
                connection.send_terminal(upload_response(flash, address, b"success\0"))
                raise InstallerError("OTA-MIDI returned verification completion without firmware-write completion. "
                                     "The terminal ACK was sent; installation was not reported as successful.")
            if address == 0xF0000000:
                if not blocks:
                    raise InstallerError("OTA-MIDI finished without requesting any firmware data.")
                connection.send(upload_response(flash, address, b"success\0"))
                c.progress(96)
                return
            if length > 32768:
                raise InstallerError("Unsupported OTA-MIDI firmware block size.")
            connection.send(upload_response(flash, address, firmware.slice(address, length)))
            self.wrote = True
            blocks += 1
            highest = max(highest, address+length)
            c.progress(min(95, highest*95/len(firmware.payload)))
        raise InstallerError("OTA-MIDI did not confirm completion.")

    def verify_reboot(self, selected):
        c = self.control
        c.status("Verifying BlackBox V20 after reboot…")
        deadline = time.monotonic()+60
        last = "Device has not returned."
        while time.monotonic() < deadline:
            connection = None
            try:
                try:
                    connection, identity, ports = self.midi.discover(selected, normal_only=True)
                except InstallerError:
                    # ALSA/CoreMIDI may assign a new port name after USB reboot.
                    # Automatic discovery is still read-only and rejects ambiguity.
                    connection, identity, ports = self.midi.discover(normal_only=True)
                if identity != ("BlackBox", 20):
                    raise InstallerError(f"Post-install identity is {identity}, expected BlackBox_020.")
                if self.hid_devices():
                    last = "USB loader is still present."
                else:
                    c.log("Normal MIDI identity BlackBox_020 confirmed; HID loader gone.")
                    c.progress(100)
                    return
            except Exception as exc:
                last = str(exc)
            finally:
                if connection:
                    connection.close()
            c.pause(1)
        raise InstallerError("Firmware transfer finished, but reboot verification failed. " + last)

    def run(self, firmware: Firmware, mode: str, selected=None):
        c = self.control
        auth_selftest()
        c.open_log(mode, firmware)
        connection = None
        loader = None
        try:
            c.check()
            if sys.platform == "win32":
                native_helper_preflight(c)
            c.status("Connecting to BlackBox…")
            if self.hid_devices():
                raise InstallerError("BlackBox is already in HID loader mode. Restart it normally before installing; automatic recovery is not assumed.")
            connection, identity, selected = self.midi.discover(selected, normal_only=True)
            if identity != ("BlackBox", 20):
                raise InstallerError(f"Expected BlackBox_020; received {identity[0]}_{identity[1]:03d}.")
            c.log("Pre-install identity: BlackBox_020; ports="+repr(selected))
            # Intentionally no host-side "same version" block and no V21 patch.
            self.prepare_midi(connection, firmware)
            connection.close()
            connection = None
            deadline = time.monotonic()+45
            paths = []
            ota_connection = None
            next_midi_probe = time.monotonic()+3
            last_snapshot = None
            while time.monotonic() < deadline:
                c.check()
                paths = self.hid_devices()
                if paths:
                    break
                if time.monotonic() >= next_midi_probe:
                    candidate = None
                    try:
                        snapshot = self.midi.ports()
                        if snapshot != last_snapshot:
                            c.log(f"Update-mode MIDI ports: input={snapshot[0]!r}; output={snapshot[1]!r}; HID loaders={len(paths)}")
                            last_snapshot = snapshot
                        try:
                            candidate, ota_identity, unused = self.midi.discover(selected)
                        except InstallerError:
                            candidate, ota_identity, unused = self.midi.discover()
                        if ota_identity[0] == "OTA-BlackBox":
                            c.log(f"OTA update-mode identity accepted: {ota_identity[0]}_{ota_identity[1]:03d}")
                            ota_connection = candidate
                            candidate = None
                            break
                    except Cancelled:
                        raise
                    except InstallerError as exc:
                        c.log("Update-mode probe: "+str(exc))
                    finally:
                        if candidate:
                            candidate.close()
                    next_midi_probe = time.monotonic()+1
                c.pause(0.1)
            if ota_connection:
                connection = ota_connection
                self.transfer_midi(connection, firmware)
                connection.close()
                connection = None
            elif paths:
                loader = HidConnection(self.hid, paths[0], c)
                self.transfer_hid(loader, firmware)
                loader.close()
                loader = None
            else:
                raise InstallerError("The device completed MIDI verification but did not enter HID 4D4A:4155 or OTA-BlackBox update mode. "
                                     "Firmware writing was not started. Restart the BlackBox normally and check the local log.")
            self.verify_reboot(selected)
            c.status("Original V20 restored." if mode == "original" else "Firmware installed. BlackBox reports V20.")
            c.log("Identity verification does not prove every flash byte; the device has no known readback API.")
        except Exception:
            c.log(traceback.format_exc())
            raise
        finally:
            if connection:
                connection.close()
            if loader:
                loader.close()
            with c.lock:
                c.critical = False
            c.emit("critical", False)
            c.close()
