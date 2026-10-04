<img width="300" height="300" alt="image" src="https://github.com/user-attachments/assets/0a71a6c2-e5d0-477f-aed7-b99361db2ec5" />

# annblackbox-custom-firmware-installer

Desktop firmware installer for the **M-VAVE BlackBox V20**.

Repository: https://github.com/entitymar/annblackbox-custom-firmware-installer

It has three primary actions:

- **Install custom firmware** — selects a compatible `.fwsc` image and sends it through the BlackBox update path.
- **Return to V20 original** — restores the included stock V20 image.
- **Cancel** — stops before the write transaction when cancellation is still safe.

## Included V20 firmware

The original M-VAVE BlackBox V20 image is stored as a normal external file:

`firmware/BlackBox_FACTORY_V20.fwsc`

SHA-256:

`c0fef191b860d3b20f77fc492a24986eef2d12df44bb5e38355684820e26cb08`

The firmware is **not embedded inside the Python source**. The restore path verifies this hash before it uses the file.

## How it works

The installer targets the BlackBox firmware-update path, not the host operating system and not the device filesystem in general.

The update path is:

`USB-MIDI -> OTA preparation -> update-mode detection -> HID or OTA-MIDI transfer -> reboot verification`

In practice:

1. The `.fwsc` container is parsed and checked before any device write starts.
2. The program identifies a normal BlackBox over USB-MIDI and expects the V20 device family (`BlackBox_020`).
3. It sends the normal OTA preparation sequence over MIDI.
4. On Windows, the bundled M-UPGRADE executable is used only as an isolated terminal-MIDI ACK helper with its matching DLL set. The M-UPGRADE graphical updater is not launched.
5. The installer waits for the device to expose an update transport. It supports the JL HID loader path and the legacy OTA-MIDI path.
6. Firmware blocks are transferred only after the update transport is established.
7. A flash is not reported as successful merely because bytes were sent. The installer waits for completion and then checks that the device returns as a normal BlackBox after reboot.

The host-side same-version rejection is not used, so a **V20 -> V20** attempt can be made. Device-side validation still applies; the installer does not fake a successful flash when the device rejects the image.

More protocol-level detail is in [TECHNICAL.md](TECHNICAL.md).

## Files that matter

- `annblackbox-custom-firmware-installer.pyw` — Windows double-click launcher.
- `annblackbox-custom-firmware-installer.py` — console/portable launcher.
- `blackbox/` — UI, protocol, resource validation and dependency setup.
- `firmware/BlackBox_FACTORY_V20.fwsc` — stock V20 restore image.
- `native/windows/M-UPGRADE/` — Windows helper and matching native DLLs.
- `dependencies/` — bootstrap/runtime assets and locally created dependencies.
- `logs/` — runtime logs generated locally after launch.

## Running

### Windows

Double-click:

`annblackbox-custom-firmware-installer.pyw`

### macOS / Linux

```bash
python3 annblackbox-custom-firmware-installer.py
```

A writable extracted folder is required. Keep the launcher, `blackbox/`, `firmware/`, `native/` and `dependencies/` together.

The bootstrap can prepare a compatible CPython/runtime and the required Python packages when the current interpreter is not suitable. First-time setup may require internet access.

Before flashing, close Altar, M-UPGRADE, DAWs and other applications that may have the BlackBox MIDI ports open.

## Read-only checks

These do not intentionally write firmware to USB:

```bash
python3 annblackbox-custom-firmware-installer.py --self-test
python3 annblackbox-custom-firmware-installer.py --setup-test
python3 annblackbox-custom-firmware-installer.py --inspect path/to/firmware.fwsc
python3 -m unittest discover -s tests -v
```

## What is intentionally not included

This package does not contain development chat transcripts, prompts, model names, private analysis notes, personal workstation logs or local usernames from the build/debug process.

It also does not claim to be an editor for arbitrary BlackBox flash memory. The installer depends on the device retaining enough of its normal boot/update protocol to accept a firmware transaction.

## Notice

This is an unofficial project and is not presented as an M-VAVE product. Firmware flashing can fail because of power loss, cable problems, incompatible images or device-side validation. Keep USB power stable and do not disconnect the device during an active write/verification stage.
