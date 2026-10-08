# ANNBLACKBOX — Custom Firmware Installer

A Windows firmware flasher for the **Sinco / M-VAVE BlackBox**. It can load a custom `.fwsc` image or attempt to restore the original **V20** firmware, including V20-to-V20 reflashing. The USB/OTA transport is **not rewritten**: the tool patches a disposable copy of the vendor's `M-UPGRADE-NTFS-BLACKBOX.exe` and lets the original updater perform the transfer.

> **Hardware-tested scope:** my own ANNBLACKBOX running the official V20 firmware. The installer includes paths intended for other installed versions, but I have **not** validated every version, hardware revision, downgrade, or custom image. A correctly sized file is not necessarily safe firmware.

## Reverse engineering / patch points

This was built from my own decompilation and analysis of two vendor host applications:

- [MVAVE M-UPGRADE — decompiled](https://github.com/entitymar/MVAVE-M-UPGRADE-decompiled)
- [MVAVE ANNBLACKBOX Firmware Update Software — decompiled](https://github.com/entitymar/MVAVE-ANNBLACKBOX-FIRMWARE-UPDATE-SOFTWARE-decompiled)

The useful finding was that the original M-UPGRADE executable already contains the BlackBox firmware as a contiguous byte range. Instead of inventing another OTA stack, I identified the embedded image and two **host-side** version gates, then kept the vendor's USB-MIDI communication, transfer sequence and logging intact.

| Location in vendor EXE | Patch | Purpose |
|---|---|---|
| `0x2E66BF` | Replace **700,436 bytes** with the selected `.fwsc` | Swap the embedded firmware payload |
| `0x31C7A` | `0F 85 13 01 00 00` → `90 E9 13 01 00 00` | Change the same-version `JNE` branch into an unconditional jump |
| `0x32762` | `7F 32` → `EB 32` | Bypass the updater's version-dependent UI gate |

Before each attempt, the tool checks the pristine executable's pinned SHA-256, restores a fresh working copy, verifies the original instruction signatures, applies the changes and reads them back. It then launches the **official updater**, triggers its update action and follows the vendor's OTA logs. These patches affect the **Windows host updater**, not the pedal's own integrity checks or bootloader.

## Use

**Requirements:** 64-bit Windows, Python 3.8+ with `tkinter`, a direct USB data connection and the bundled vendor updater. No internet connection or administrator access is required for flashing. Windows 10/11 are the recommended targets; compatibility with every Windows release has not been physically verified.

1. Connect the BlackBox by USB and run `annblackbox-custom-firmware-installer.py`.
2. Choose **Install custom firmware** to select a 700,436-byte `.fwsc`, or **Restore V20 factory** to use `firmware/BlackBox_FACTORY_V20.fwsc`.
3. Confirm and leave the pedal connected until the updater finishes and the device reboots. **Cancel** closes the tool; interrupting an active write is unsafe.

CLI diagnostics and flashing:

```powershell
python annblackbox-custom-firmware-installer.py --selftest
python annblackbox-custom-firmware-installer.py --probe
python annblackbox-custom-firmware-installer.py --flash factory
python annblackbox-custom-firmware-installer.py --flash "C:\path\to\custom.fwsc"
```

`--selftest` checks patch mechanics on a temporary executable; `--probe` launches the updater without flashing. Neither replaces a physical flash test. Tool logs are saved under `logs/`; original updater logs remain under `windows/official-updater/LOG/`.

If the updater hits the known **Verification timeout** after an interrupted cycle, the tool attempts to complete the pending transfer using its available recovery candidates (bundled custom/factory image and last selected image), then retries the requested flash. **Recovery is best-effort, not a guaranteed unbrick method.** Do not disconnect USB during writing; if recovery fails, stop and inspect the logs before trying again.

## A personal note

I reverse-engineered the updater software myself because I wanted a more flexible, reversible way to work with my BlackBox without replacing a working OTA protocol. **DeepSeek 4.1 Flash assisted me** with parts of the development, but I did not want to publish something based only on generated code or assumptions.

I tested this repeatedly on **my own ANNBLACKBOX with official V20**. During development I managed to **soft-brick the pedal several times**, and getting it working again was part of learning where the process actually fails. I kept adjusting the method and checking it on real hardware; it is now stable in the V20 scenarios I have personally tested, not magically proven safe for every setup.

I will improve it further as I can. Please be patient: my testing is slow on purpose. With firmware, I would rather verify things properly than rush out **AI slop** that looks convincing but puts someone else's device at risk.

## Risk and attribution

**Unofficial project.** Not affiliated with, authorized, endorsed or supported by M-VAVE, Sinco or related companies. The original OTA engine belongs to its vendor; this tool only modifies a working copy of its Windows updater. All trademarks belong to their owners.

Provided **as is, without warranty**. Flashing may cause data loss, invalidate warranties or render the pedal unusable. **Use entirely at your own risk.** Keep a known-good firmware image, do not interrupt the transfer and do not assume cross-version restores are guaranteed.
