<img width="300" height="300" alt="image" src="https://github.com/user-attachments/assets/0a71a6c2-e5d0-477f-aed7-b99361db2ec5" />

# annblackbox-custom-firmware-installer

Desktop firmware installer for the **M-VAVE BlackBox V20**.

Repository: https://github.com/entitymar/annblackbox-custom-firmware-installer · License: MIT

It has three primary actions:

- **Install custom firmware** — selects a compatible `.fwsc` image and sends it through the BlackBox update path.
- **Return to V20 original** — restores the included stock V20 image (`firmware/BlackBox_FACTORY_V20.fwsc`).
- **Cancel** — stops before the write transaction when cancellation is still safe.

## Contents

- [Requirements](#requirements)
- [Reverse engineering provenance](#reverse-engineering-provenance)
- [Repository layout](#repository-layout)
- [Architecture](#architecture)
- [The FWSC firmware container](#the-fwsc-firmware-container)
- [How an install works, stage by stage](#how-an-install-works-stage-by-stage)
- [Cancellation and safety model](#cancellation-and-safety-model)
- [Logging](#logging)
- [Integrity and verification](#integrity-and-verification)
- [Running](#running)
- [Command line](#command-line)
- [Tests](#tests)
- [Notice](#notice)

## Requirements

### Supported desktops

| Platform | Support |
|---|---|
| Windows x64 | Yes (WinMM MIDI backend; ARM64 runs under x64 emulation) |
| macOS 10.12+ (x64) / 11+ (Apple Silicon) | Yes |
| Linux x64 / ARM64 with glibc | Yes (musl/Alpine is rejected: the pinned Qt/MIDI wheels need glibc) |

The program must live in a **writable extracted folder**: its runtime, logs and caches are created next to the launcher, never in system locations.

If the starting interpreter is not suitable, the bootstrap downloads a compatible CPython and the pinned wheels from PyPI (see [Architecture](#architecture)); first-time setup then needs internet access. Subsequent starts reuse the cached runtime with no network access.

### Python

- **Native interpreter path:** CPython 3.10–3.12, 64-bit, GIL enabled (`compatible_python()` in `blackbox/setup.py`).
- **Managed path (anything else, including 3.13+ or 32-bit):** the bootstrap provisions a uv-managed **CPython 3.12** under `dependencies/python/` and a private virtual environment under `dependencies/`.

### Python packages (`requirements.txt`, binary wheels only)

| Package | Pin | Used for |
|---|---|---|
| `PySide6-Essentials` | 6.8.3 | Qt Widgets GUI |
| `python-rtmidi` | 1.5.8 | USB-MIDI I/O (forced to the WinMM backend on Windows) |
| `hidapi` | 0.14.0.post4 | JL HID loader transport |

### Device side

- A BlackBox connected by USB. Before flashing, close M-UPGRADE, DAWs and any other application holding the BlackBox MIDI ports open.
- Linux only, if the HID loader is not accessible: install `linux/99-blackbox.rules` into `/etc/udev/rules.d/` (udev rule granting `uaccess` to the loader `4d4a:4155`).

## Reverse engineering provenance

The BlackBox has no public protocol documentation. Every protocol detail implemented in this installer — the FWSC container layout, the OTA SysEx sequence and its address semantics, the JL HID framing and RCSP opcodes, the loader challenge-response authentication, and the exact update timings — was recovered by **reverse engineering the official M-VAVE Windows update tools**. The decompilation work lives in these companion repositories:

- **https://github.com/entitymar/MVAVE-M-UPGRADE-decompiled** — decompilation of `M-UPGRADE.exe`, the vendor updater this project started from. Code comments in `blackbox/core.py` still cite its functions by address, for example:
  - `FUN_14001dbe0` / `FUN_14001dd20` — the FWSC wrapper parsers (36-slot, then 20-slot) that `Firmware.parse()` reimplements;
  - `FUN_140018380` — case-insensitive identity-prefix comparison (why `ota-BlackBox_021` from the real bootloader is accepted);
  - `FUN_140012740` — legacy OTA-MIDI timing: wait 3 s, resend the OTA-enter SysEx, wait 2 s;
  - `FUN_14001f6e0` — `0xC…` state requests are answered over the still-open main ports.
- **https://github.com/entitymar/MVAVE-ANNBLACKBOX-FIRMWARE-UPDATE-SOFTWARE-decompiled** — decompilation/disassembly and resource extraction of the M-VAVE firmware update software, used as the cross-check corpus for the update flow and framing.

The Python reimplementation of the authentication cipher was validated round-by-round (100 intermediate-state comparisons) against the native code executing on x64; the recorded preparation traffic replayed in `tests/fixtures/preparation_requests.json` was captured from the real update flow (see `VALIDATION.json`).

## Repository layout

```
annblackbox-custom-firmware-installer.py    # CLI launcher (also the terminal-ACK child)
annblackbox-custom-firmware-installer.pyw   # Windows double-click launcher (no console)
blackbox/
  app.py          # PySide6 window, command line entry points
  core.py         # FWSC parsing, MIDI/HID framing, JL auth, transfer workflow
  resources.py    # paths, pinned hashes, exception types, stock firmware loader
  setup.py        # runtime/dependency bootstrap (venv or uv-managed CPython 3.12)
firmware/BlackBox_FACTORY_V20.fwsc          # stock V20 image (external file, SHA-pinned)
native/windows/M-UPGRADE/                   # bundled vendor updater material (see Integrity)
linux/99-blackbox.rules                     # optional udev rule for the HID loader
licenses/                                    # Qt commercial license notice
dependencies/                                # created at runtime (venv, uv, Python, pip cache)
logs/                                        # per-session logs, setup.log, startup.log
tests/                                       # offline unittest suite + recorded traffic fixture
requirements.txt · SHA256SUMS.txt · VALIDATION.json · LICENSE
```

## Architecture

The `.pyw` launcher just runs the `.py` launcher, which puts the application directory first on `sys.path` and delegates to `blackbox.app.main()`. If the launcher itself crashes, it writes `logs/startup.log` and shows a Tkinter error dialog — the log always exists even when the GUI cannot start.

There are four modules:

| Module | Responsibility |
|---|---|
| `blackbox/setup.py` | Decides whether the current interpreter can host the app; if not, builds a private one and relaunches itself. |
| `blackbox/app.py` | Parses CLI flags; builds the Qt window; runs `Installer.run()` on a worker thread, marshalling status/progress/critical events back to the UI through a queue polled by a 50 ms timer. |
| `blackbox/core.py` | The protocol engine: FWSC container parsing, SysEx framing, JL challenge-response, RCSP/HID framing, and the `Installer` state machine. |
| `blackbox/resources.py` | Application-local paths, pinned SHA-256 constants (`STOCK_SHA`, `BRIDGE_SHA`), the exception hierarchy, and `factory_bytes()` which reads and hash-checks the stock image. |

### Bootstrap (`ensure_dependencies`)

1. If the current interpreter is a compatible CPython with all pinned packages importable at the exact pinned versions, the app starts directly.
2. Otherwise a private environment is built under `dependencies/`:
   - **Native interpreter:** a venv created with the standard library, named `runtime-<Platform>-<machine>-<pyver>`.
   - **Incompatible interpreter:** `managed_platform()` maps the OS/arch to a supported target (Windows ARM64 is mapped to x64), then a pinned **uv 0.12.23** wheel is fetched from the PyPI JSON API and accepted only if its SHA-256 matches the hard-coded per-platform pin and the URL host is `files.pythonhosted.org`. uv then provisions CPython 3.12 into `dependencies/python/` and seeds `runtime-managed-3.12-<os>-<arch>`.
3. Dependencies are installed with `pip --isolated --only-binary=:all:` from `https://pypi.org/simple` — exact pins from `requirements.txt`, wheels only, no source builds.
4. The finished environment is validated by running `<python> -I <entrypoint> --check-dependencies` (version pins + import smoke test). The app then relaunches itself as `python -I <entrypoint> <original args>` with `BLACKBOX_READY=1` (using `pythonw.exe` on Windows when there is no console), so the installed app always runs isolated (`-I`) inside its own environment.
5. While setup runs, a stdlib Tkinter splash shows progress and offers Cancel; a full setup log goes to `logs/setup.log`.

A cached runtime short-circuits all of this and starts offline with zero downloads.

## The FWSC firmware container

`Firmware.parse()` (in `core.py`) accepts files from 2 KiB to 16 MiB and validates everything before any device session starts:

1. **Outer wrapper.** Two layouts are tried, 36-slot first then 20-slot. Each slot is 48 bytes; byte 47 of each slot carries an obfuscated character — `chr((value - index - 1) & 0xFF)` — until a `0x7D` sentinel terminates the string; every byte after the sentinel must also be `0x7D`. The decoded string must match `BlackBox_([0-9]{3})`. Only **V20** is accepted; a wrapper advertising another version is rejected.
2. **Payload assembly.** The payload is the first 47 bytes of every slot concatenated, plus everything after the slot table.
3. **JL header.** The first 64 payload bytes are de-obfuscated with `jl_xor()`: a keystream generated by a CRC-16/XMODEM-style LFSR (poly `0x1021`, seed `0xFFFF`) XORed byte-wise; the stream resets at each header/record boundary. The header is `struct "<HHIHHI"` → `hcrc, table_crc, size, count, flags, table_end`:
   - `crc_hqx(header[2:], 0)` must equal `hcrc`;
   - `size` must equal the exact payload length;
   - `count` (file table entries) must be 1–64;
   - `header[16:32]` must start with `"AC791N"` (the BlackBox SoC family).
4. **File table.** `count` records of 80 bytes, each `jl_xor`-decoded: `struct "<HHIIII"` → `tag, seq, checksum, offset, length, reserved`, followed by a 16-byte NUL-terminated ASCII name at record offset 64. Every record is bounds-checked (`seq == index`, in-payload offsets, `length <= reserved`).
   - CRC-16 (`crc_hqx`) is verified **only** for `flash.bin`, `ota.bin`, `tail.bin`, where the CRC covers the stored unpadded bytes. Other files use a different per-file encoding, so their CRC is deliberately not guessed.
5. **Required components and tail.** `flash.bin`, `ota.bin` and `tail.bin` must all be present, and `"JLUFW"` must appear in the last 64 bytes of the payload.

The parsed result keeps `(name, offset, length, checksum)` sections plus a `slice(offset, length)` accessor that serves device pull requests with strict bounds checking — the device can never make the host read outside the file.

Two SHA-256 constants get special treatment at runtime: `STOCK_SHA` (the exact factory image, enforced every time the stock firmware is loaded) and `BRIDGE_SHA` (an experimental bridge image where one internal tag changes to `021`; the GUI flags it as adding no custom features).

## How an install works, stage by stage

`Installer.run()` in `blackbox/core.py` drives the whole transaction:

`USB-MIDI → OTA preparation → update-mode detection → HID or OTA-MIDI transfer → reboot verification`

It only uses the update interfaces the BlackBox firmware and bootloader already expose. No filesystem mounting, no WinUSB/Zadig driver replacement, no hidden updater windows.

### Stage 0 — Pre-flight

- `auth_selftest()` runs a known-answer test of the JL authentication function (`auth_response(bytes(range(16)))` must equal `e5807e887bde07630b4d0e715b3e8991`). If it fails, no device operation starts.
- A session log is opened (see [Logging](#logging)).
- If a HID loader (`VID 0x4D4A:PID 0x4155`) is *already* visible, the device is stuck in loader mode: the installer refuses to start and asks for a normal restart. Automatic recovery is never assumed.

### Stage 1 — Discovery and identity over USB-MIDI

`MidiDevices.discover()` enumerates rtmidi ports and pairs candidates by name tokens (`blackbox`, `usb-midi`, `sinco`, `m-vave`, …). Each candidate pair is opened and probed with the identity query SysEx:

```
F0 00 32 45 00 00 00 40 7F F7
```

(which is the 7-bit packing of an empty `0x11` frame). The reply is a `0x11` frame whose body is an ASCII identity such as `BlackBox_020` or `ota-BlackBox_021`; the prefix comparison is case-insensitive, matching the vendor bootloader. The install requires exactly **one** responder with identity `("BlackBox", 20)`. More than one responder, more than 16 candidate pairs, or a port set that changes mid-detection are all hard errors; the user can also pin an exact input/output pair by port *name* (names, not indices, survive USB re-enumeration) via the **USB connection** dialog.

MIDI framing used everywhere: bodies are packed 8→7 bits little-endian (`pack7`/`unpack7`) inside SysEx, and the inner frame is `00 59 <cmd> <len:3 LE> <body> <(~sum(body)) & 0xFF>`. The upload protocol is command `0x30`:

- request body: `<flash-type byte> <address:4 LE> <length:3 LE>`
- response body: the same header plus the data bytes.

### Stage 2 — OTA preparation over MIDI

`prepare_midi()` sends the enter-OTA SysEx `F0 22 24 35 7F F7`, then serves the device's `0x30` request loop (deadline 120 s). The address field is a command channel:

| Address pattern | Meaning | Installer behavior |
|---|---|---|
| `0xC………` (`addr & 0xF0000000 == 0xC0000000`) | handshake / state request | reply `"success\0"` |
| `0xD………` (`addr & 0xFF000000 == 0xD0000000`) | device-side rejection | reply `"success\0"`, then fail: `0xD0000083` = firmware identical to installed, low byte `0x97` = image not accepted by this device, anything else = verification failed |
| `0xE0000000` | preparation/verification complete | send the isolated terminal ACK (below) and move on |
| `0xF0000000` | write complete (legacy transport only) | unexpected at this stage → error |
| anything else | firmware pull: `address` = payload offset, `length` ≤ 32768 | reply with `firmware.slice(address, length)` |

Note the **same-version policy**: the host deliberately does not pre-reject a V20 image on a V20 device, so a V20→V20 attempt reaches the device — but the device's own validation still applies, and a device rejection (typically `0xD0000083`) is reported as a failure, never faked as success.

### Stage 3 — Update-mode detection

After the terminal ACK the device leaves normal MIDI mode. For up to 45 s the installer polls (about once per second, cancel-aware in between):

- the JL HID loader appearing at `VID 0x4D4A:PID 0x4155` (exactly one loader interface allowed), **or**
- a MIDI device identifying as `OTA-BlackBox_*` (the bootloader reports its own version, e.g. `021`, which is accepted here).

If neither appears, the install fails explicitly: "firmware writing was not started."

### Stage 4a — JL HID loader transfer (`transfer_hid`)

The HID transport speaks **RCSP** inside 64-byte JL reports:

- Report frame: `"JL" <kind<<4> 0x00 <len:2 BE, includes the message byte> <message> <data> 0xED`, zero-padded to a 64-byte multiple; the receiving `HidDecoder` reassembles frames across reports and enforces the `0xED` trailer.
- RCSP packet: `FE DC BA <flags> <opcode> <len:2 BE> <body> EF`, where flags `0xC0`/`0x80` mark a command (with/without reply-request) and `0x00` a reply; a command body is `<seq> <data>`, a reply body is `<status> <seq> <data>`.

**Authentication** (`authenticate()`): the JL challenge-response, reimplemented from the vendor loader. First a fixed reset packet `FEDCBAC00600020001EF`; then the host sends a random 16-byte challenge and verifies the device's `\x01`-prefixed answer with `auth_response()`, a deterministic 16-byte SPN-style construction (key schedule over `AUTH_KEY` with the `AUTH_BIAS` table, eight rounds mixing adds/XORs, GF(257) exp/log tables and a fixed permutation, whitening, then a second eight-round pass with a transformed key and challenge/MAC mixing). The device then issues its own counter-challenge, which the host answers the same way; both directions must end in `\x02pass`.

**Transfer** then proceeds by RCSP opcode:

| Opcode | Direction | Meaning |
|---|---|---|
| `0xE1` | device → host | config request: asks for `<offset:4 BE> <length:2 BE>` of the payload |
| `0xE2` | host → device | config data: host sends the requested slice; device replies status `0x00` = accepted, anything else = rejected (install aborts, nothing was written) |
| `0xE3` | host → device | **start the write transaction** — the commit point |
| `0xE5` | device → host | firmware block request `<offset:4 BE> <length:2 BE>` (up to 65524 bytes); host answers with the data |
| `0xE8` | device → host | write complete; host ACKs |

Progress on this path is computed from the *union of requested byte ranges* (merged intervals) over the payload size — re-requests do not inflate the bar. On `0xE8` the ACK is sent, but a `TransportLost` while sending it is tolerated: the device may reboot and drop the HID interface during the ACK, and success is still gated by Stage 5. Any other unexpected opcode is answered with status `1` and fails the install.

### Stage 4b — Legacy OTA-MIDI transfer (`transfer_midi`)

Used **only** when Stage 3 found an `OTA-BlackBox` identity. It mirrors the vendor updater's timing (`FUN_140012740`): wait 3 s, resend the enter-OTA SysEx, wait 2 s, then serve the same `0x30` request loop as Stage 2 (deadline 240 s), with two differences:

- `0xF0000000` is the write-completion marker: the installer replies `"success\0"` over the still-open ports and finishes successfully — but only if at least one firmware block was actually pulled ("finished without requesting any firmware data" is an error).
- `0xE0000000` on this path means the device finished *verification* without a write: the terminal ACK is still delivered, but the install is reported as **not successful**.

### The isolated terminal ACK

The `0xE0000000` completion ACK must be delivered after the main process has released both MIDI ports, so it is sent by an **isolated child process of this same program; no bundled executable is launched**. `send_terminal()`:

1. Validates the outgoing packet with `terminal_sysex()`, which accepts **only** an E/F completion ACK with body `"success\0"` — nothing else can ever be routed through this path.
2. Closes both MIDI ports strictly; if a port cannot be released the install fails *before* any ACK is sent.
3. Spawns a short-lived child — `python -I <entrypoint> --terminal-ack <output-name> <hex>` — which opens exactly that one MIDI output by name, sends the single SysEx, and exits. On Windows the child forces the WinMM backend and is created without a console window.
4. Waits up to 10 s for the child; no automatic retry is ever performed.

### Stage 5 — Reboot verification (`verify_reboot`)

A transfer is never called successful merely because bytes were sent. For up to 60 s the installer rediscovers the normal-mode device — first the pinned port pair by name, falling back to automatic discovery if the OS assigned new port names — and requires:

- identity `("BlackBox", 20)` over MIDI, **and**
- the HID loader gone.

Only then does the UI report success (progress 100). One honest caveat, also written to the log: this proves the device rebooted into a normal V20 identity, not that every flash byte matches — the device exposes no known flash-readback API.

## Cancellation and safety model

- The worker checks `Control.check()` between every operation (each MIDI/HID exchange, each polling loop), so a cancel takes effect at the next safe boundary.
- **Cancel is available** during discovery, preparation and config validation. The window closes when `begin_critical()` runs: immediately *before* the `0xE3` commit on the HID path, and before the first write request on the legacy path. From that point the UI disables Cancel and window-close with the message that cancellation is unavailable until reboot verification finishes.
- There are **no automatic write retries**: a failed or interrupted transaction surfaces as an error. An unexpected disconnect before a completion indication is a failure, not a success.
- Exception hierarchy: `InstallerError` (expected, user-facing message), `Cancelled` (subclass), `TransportLost` (USB I/O lost mid-transaction). Every failure writes the full traceback to the session log.

## Logging

Every install session writes `logs/<YYYYMMDD-HHMMSS>-<token>.log` inside the application folder (never system-wide): monotonic timestamps, platform/Python versions, the firmware SHA-256 and wrapper details, every MIDI/HID request and block, identity transitions, and the final outcome. `logs/setup.log` holds the bootstrap transcript and `logs/startup.log` launcher crashes. The distributed package ships no previous workstation/session logs.

## Integrity and verification

- `SHA256SUMS.txt` is the manifest of the distributed package (launcher, modules, stock firmware, native files, tests).
- The stock image's SHA-256 (`c0fef191…cb08`) is pinned in the code and re-verified on every use; a damaged file is refused.
- The uv bootstrap wheel is pinned per-platform by SHA-256 and only accepted from `files.pythonhosted.org`.
- `native/windows/M-UPGRADE/` bundles the **unmodified vendor updater material** (with its `FILES.json` manifest) for provenance and manual recovery scenarios. The installer does **not** execute `M-UPGRADE.exe` — the isolated terminal ACK is performed by the installer's own child process. The only trace of M-UPGRADE in a session is the reminder to close it before flashing, because it holds the MIDI ports.
- `VALIDATION.json` records the offline validation status (protocol tests, recorded-traffic replay, bootstrap tests, known-answer vectors).

## Running

### Windows

Double-click `annblackbox-custom-firmware-installer.pyw`.

### macOS / Linux

```bash
python3 annblackbox-custom-firmware-installer.py
```

Keep the launcher, `blackbox/`, `firmware/`, `native/` and `dependencies/` together in the writable folder.

The GUI offers the three primary actions, a progress bar with live status, a **USB connection** dialog for pinning an exact MIDI port pair (automatic detection by default), and a Cancel button. `--demo` previews the window with USB writes disabled.

## Command line

| Flag | Effect |
|---|---|
| `--self-test` | Offline checks only: auth known-answer test plus a full parse of the bundled stock image; prints JSON. No dependencies installed, no USB writes. |
| `--setup-test` | Runs the automatic setup, then the self-test. No USB writes. |
| `--inspect FWSC` | Parses any `.fwsc` and prints name, version, SHA-256, size and file table as JSON. No USB. |
| `--export-original PATH` | Writes the stock V20 FWSC to PATH (fails if the file exists; never touches USB). |
| `--demo` | Opens the window in preview mode with USB writes disabled. |
| `--terminal-ack OUT HEX` | Internal: the isolated ACK child described above. |
| `--check-dependencies` | Internal: exit code 0 when the runtime is ready. |

Read-only examples:

```bash
python3 annblackbox-custom-firmware-installer.py --self-test
python3 annblackbox-custom-firmware-installer.py --setup-test
python3 annblackbox-custom-firmware-installer.py --inspect path/to/firmware.fwsc
python3 -m unittest discover -s tests -v
```

## Tests

`tests/test_installer.py` is fully offline — no physical device is touched. It covers:

- **Protocol units:** FWSC parsing (bit-flip rejection at several offsets, non-V20 rejection, truncation), 7-bit codec round-trips, MIDI/RCSP framing corruption, HID fragmentation across report boundaries, authentication known-answer vector, out-of-bounds slice requests.
- **Integration:** the full `Installer.run()` flow against fake MIDI/HID transports simulating the device — happy path via HID, legacy OTA-MIDI path, device-side same-version rejection (`0xD0000083`), config rejection before any write, cancellation before the `0xE3` commit, cancellation before USB, and the constraint that the isolated ACK runs only after both ports closed and never retries.
- **Bootstrap:** runtime-compatibility decisions (3.14/32-bit need the managed runtime), tampered-download rejection, verified download + offline reuse, cached-runtime launch without downloads, cancelled setup starting no process.
- **Resources/terminal:** stock firmware exactness, no embedded fallback, corrupted-file rejection, logs staying inside the program folder, and replay of the recorded real preparation traffic in `tests/fixtures/preparation_requests.json`.

## Notice

This is an unofficial project and is not presented as an M-VAVE product. Firmware flashing can fail because of power loss, cable problems, incompatible images or device-side validation. Keep USB power stable and do not disconnect the device during an active write/verification stage.
