# ANNBLACKBOX — Custom Firmware Installer

Windows tool that flashes firmware onto the Sinco **BlackBox** pedal and restores the
factory **V20** at any time, no matter which version is installed (v20, v21, v22, v30…).
It does not reimplement the OTA protocol: it patches a working copy of the official
ANNBLACKBOX updater (`M-UPGRADE-NTFS-BLACKBOX.exe`), launches it and follows the flash
through the updater’s own log files.

## What it is

Three buttons:

| Button | Action |
|---|---|
| **Install custom firmware** | Opens a file picker to choose the `.fwsc` to flash. Any BlackBox firmware works (must be exactly 700,436 bytes); the dialog starts in `firmware/`. |
| **Restore V20 factory** | Flashes `firmware/BlackBox_FACTORY_V20.fwsc`. Works from any installed version. |
| **Cancel** | Closes the tool (asks first while a flash is running). |

## Requirements

* Windows 8 / 10 / 11 — 64-bit.
* Python 3.8 or newer, from <https://www.python.org/downloads/windows/>
  (official installer, with the default *tcl/tk and IDLE* option; tick
  *Add python.exe to PATH*). If tkinter is missing the tool tries to install it
  (`pip install --user tk`); if that fails it shows a native error window with the fix.
* No admin rights and no internet connection are needed to flash.
* ~150 MB of disk (vendor updater ~71 MB + firmware + logs).
* Pedal connected directly to a USB port with a data cable (no unpowered hub during a flash).

## How to use

1. Plug the pedal in and wait for it to enumerate (shows up as `USB-Midi` / `BlackBox`).
2. Double-click `annblackbox-custom-firmware-installer.py`.
3. **Install custom firmware** → pick the `.fwsc` → confirm — or **Restore V20 factory** →
   confirm.
4. Wait until the tool reports the result (a message box appears if something fails).

### Logs

Each run writes `logs/annblackbox-<date>-<time>-<kind>.log` with the tool’s messages and
every line the updater writes. The **logs** link in the footer opens that folder. The
updater also keeps its own logs in `windows/official-updater/LOG/`.

### Console mode

```
python annblackbox-custom-firmware-installer.py --flash factory       # factory V20
python annblackbox-custom-firmware-installer.py --flash "C:\path\to\my.fwsc"
python annblackbox-custom-firmware-installer.py --selftest            # patch engine check
python annblackbox-custom-firmware-installer.py --probe               # open+close updater, no flash
```

## How it works

1. Restores the pristine updater
   (`windows/M-UPGRADE-NTFS-BLACKBOX.exe.pristine`, SHA-256 pinned `9444eb6e…97b8`) over
   the working copy and applies three in-place patches:
   * **firmware swap** — the chosen `.fwsc` is written over the 700,436-byte firmware
     block embedded in the exe at `0x2E66BF`;
   * **same-version guard bypass** — `0F 85 13 01 00 00` → `90 E9 13 01 00 00` at `0x31C7A`;
   * **UI gate bypass** — `7F 32` → `EB 32` at `0x32762`.
   Every write is read back and verified.
2. Launches the updater and activates its **Update the firmware** button.
3. Reads the updater’s log files and reports progress and the final result.

### Versions

The firmware version is stored only in the file header, and the updater has no downgrade
lock; the tool bypasses the two version checks above regardless. Restoring V20 therefore
works from any installed version (v21, v22, v30, custom builds).

### Self-recovery

If a write is interrupted mid-transfer (for example the tool is closed while flashing),
the pedal keeps a pending update cycle pinned to the interrupted file. While that is the
case, a flash of any other file stalls at the updater’s own
*“Verification timeout! Please power cycle the device and try again.”* step, and nothing is
written. The tool detects this and, in order:

1. closes the stuck updater window and stops its process (a leftover process keeps the exe
   locked, which would block patching);
2. flashes the firmware the pedal is pinned to — it tries the `.fwsc` files in `firmware/`
   and the last file you flashed, until one completes the pending cycle;
3. re-runs the flash you asked for.

Leftover updater windows and processes are also cleared before every attempt. The bundled
firmwares are embedded in the tool and re-created if the files in `firmware/` go missing.

## Safety notes

* Do not unplug USB while flashing. The pedal has a battery — unplugging does not power it
  off.
* Do not close the tool while the firmware write is in progress (it warns you if you try):
  cancelling mid-write leaves the pending cycle described above.
* After the write the pedal reboots; USB can disappear for 1–3 minutes and then come back.
* If a flash fails twice in a row (even after the automatic recovery), power-cycle the
  pedal (hold its power button until OFF, leave it 2–3 minutes, then boot and reconnect
  USB) and run the tool again.

## Files

```
annblackbox-custom-firmware-installer.py    the whole tool (stdlib only; embeds fallback copies of the firmware files)
firmware/                                   put the .fwsc firmware files here (any BlackBox firmware)
firmware/BlackBox_FACTORY_V20.fwsc          factory V20 — used by "Restore V20 factory" (sha256 c0fef191…cb08)
logs/                                       one persistent log per run (created at run time)
windows/M-UPGRADE-NTFS-BLACKBOX.exe.pristine   untouched vendor binary (sha256 9444eb6e…97b8)
windows/official-updater/                   the official updater (Qt) — working copy
LICENSE
```

Presets are not touched by a flash — only the firmware is replaced.

## Disclaimer

This is **not an official M-VAVE product** and it is **not affiliated with, authorized,
endorsed or supported by M-VAVE, Sinco, or any related brand or company**. All product
names, trademarks and brands belong to their respective owners.

It is provided **“as is”, without warranty of any kind**. You use it **entirely at your own
risk and precaution**:

* flashing firmware can damage or brick your pedal if something goes wrong (power loss,
  unplugging during the write, third-party software, etc.);
* modifying or flashing firmware may **void your warranty**;
* the author is **not responsible** for any damage to your device, data loss, or any other
  consequence of using this tool.

By downloading, running or using this tool you accept these terms.

Note: the OTA engine is the original vendor updater (M-UPGRADE-NTFS-BLACKBOX, Sinco); this
tool only patches its embedded firmware and two guard branches at run time.
