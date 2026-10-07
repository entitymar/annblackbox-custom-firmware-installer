# ANNBLACKBOX — Custom Firmware Installer

Flash a **custom firmware** onto the Sinco **BlackBox** pedal — and go back to the
**factory V20** at any time, **no matter what version you flashed before** (v21, v22, v30…)
— using the **official ANNBLACKBOX updater engine** itself, fully automatic.

**created by entitymar** — <https://entitymar.pages.dev/>
**GitHub repo** — <https://github.com/entitymar/annblackbox-custom-firmware-installer>

---

## What it is

A small Windows tool (Python + tkinter, standard library only) with exactly three actions:

| Button | What it does |
|---|---|
| **Install custom firmware** | Opens a **file picker** so you can choose the `.fwsc` to flash — the dialog starts in `firmware/` with the bundled custom preselected, but any BlackBox firmware file works (must be exactly 700,436 bytes: another custom build, the factory file, a v21/v30 build, …) |
| **Restore V20 factory** | Flashes the factory `firmware/BlackBox_FACTORY_V20.fwsc` back — works from **any** installed version |
| **Cancel** | Closes the tool (asks first if a flash is running) |

The interface keeps the black/grey look of the official updater. Everything (patching,
launching, clicking the updater, watching the flash, recovering from stalls) is automatic:
**the mouse cursor never moves**.

## Requirements (the machine that runs it)

* **Windows 8 / 10 / 11 — 64-bit.**
* **Python 3.8 or newer** for Windows, from <https://www.python.org/downloads/windows/>
  (the official installer includes Tcl/Tk — keep the *“tcl/tk and IDLE”* option checked and
  tick *“Add python.exe to PATH”*).
  * If tkinter (the GUI part) is missing, the tool **tries to install it automatically**
    (`pip install --user tk`); if that is not possible it shows a native error window with
    the exact fix.
* **No admin rights** and **no internet** are needed to flash (internet is only used by the
  optional dependency bootstrap above).
* About **150 MB of disk** (vendor updater ~71 MB + firmwares + logs).
* The pedal connected **directly** to a USB port with a data-capable cable (avoid unpowered
  USB hubs during a flash).
* A working antivirus is fine, but if it ever quarantines the patched updater
  (`windows/official-updater/M-UPGRADE-NTFS-BLACKBOX.exe`), allow it — the tool rewrites
  that file before every flash.

Nothing else is installed on the system; the vendor updater runs from its own folder.

## How to use

1. Plug the pedal in via USB and wait for it to enumerate (it shows up as `USB-Midi` /
   `BlackBox`).
2. Double-click **`annblackbox-custom-firmware-installer.py`**.
3. **Install custom firmware** → pick your `.fwsc` → confirm — or **Restore V20 factory** →
   confirm.
4. The official updater window opens and the flash **starts by itself**. Do not touch
   anything until the tool reports the result.

### Logs

Every run writes a complete log to **`logs/annblackbox-<date>-<time>-<kind>.log`** (the
tool’s own messages plus **every raw line** the official updater writes). The **“logs”**
link in the footer opens that folder — useful to check what happened or to send when
reporting an issue. The updater also keeps its own logs in `windows/official-updater/LOG/`.

### Console mode

```
python annblackbox-custom-firmware-installer.py --flash custom        # bundled custom
python annblackbox-custom-firmware-installer.py --flash factory       # factory V20
python annblackbox-custom-firmware-installer.py --flash "C:\path\to\my.fwsc"
python annblackbox-custom-firmware-installer.py --selftest            # patch engine check
python annblackbox-custom-firmware-installer.py --probe               # open+close updater, no flash
```

## How it works (exactly like the official updater)

The vendor binary `M-UPGRADE-NTFS-BLACKBOX.exe` carries its firmware image **embedded**,
in one contiguous **700,436-byte** block at offset `0x2E66BF`, and refuses some writes
with *“Cannot update the same firmware!”* / *“Already the latest version”*.

Before every flash the tool **restores a pristine copy** of the vendor exe and applies
three verified, byte-for-byte in-place patches to the working copy
(`windows/official-updater/`):

1. **Firmware swap** — the chosen `.fwsc` (any version: custom, factory, v21/v30 builds)
   is written over the embedded block at `0x2E66BF` (same size, so nothing else moves).
2. **Same-version guard bypass** — `0F 85 13 01 00 00` → `90 E9 13 01 00 00` at `0x31C7A`
   (the start-time version check always passes).
3. **UI gate bypass** — `7F 32` → `EB 32` at `0x32762` (the button is never greyed out).

Then it launches the vendor exe and activates **“Update the firmware”** with a *posted*
(synthetic) click — Windows message queue only, so **the visible cursor never moves and no
window is dragged around**. The tool follows the vendor logs and reports success/failure
in real time. Every patch write is **read back and verified** before the updater is
launched, and the pristine binary is SHA-256-pinned (`9444eb6e…97b8`).

### Versions & the V20 guarantee

* The firmware **version lives only in the file’s header**, not in the payload, and there
  is **no downgrade lock** in the updater (the only version guards are the two above,
  and a downgrade — older file over newer pedal — already passes the vendor’s own logic).
* **Restore V20 works no matter what you installed before** — v21, v22, v30, your own
  builds.
* The pedal may keep a **pending update cycle** from an interrupted write. While it does,
  a flash of a *different* file stalls at the updater’s own
  *“Verification timeout! Please power cycle the device…”* step. The tool now handles
  that by itself (below), so restoring V20 always succeeds.

### Self-recovery (no more dead ends)

When the tool sees that stall it automatically:

1. closes the stuck updater window **and stops its process** (a leftover process keeps the
   exe locked — this used to make retries fail),
2. finds the file the pedal is pinned to and **completes the pending cycle** by flashing
   it — it tries every candidate in order: the bundled custom firmware, the bundled
   factory, and the last file you flashed (this run always flies clean),
3. re-runs the flash you asked for — which then also flies clean.

You just see extra progress lines in the log; the final result is the firmware you chose
(*“flashed successfully after auto-recovery of a pending firmware cycle”*). Leftover
updater windows from previous attempts are also closed automatically, and both bundled
firmwares are **embedded in the tool**: if `firmware/` loses one, it is restored
automatically before flashing.

## Safety notes

* Do **not** unplug USB while flashing. The pedal has a battery — unplugging does not
  power it off.
* Do not close the tool while the firmware **write** is in progress (it warns you if you
  try): cancelling mid-write leaves the pending cycle described above (the tool can still
  recover it, but it takes longer).
* After the write the pedal reboots: **USB may disappear for 1–3 minutes** and then come
  back on its own. That is normal.
* If a flash ever fails twice in a row (even after the automatic recovery), power-cycle
  the pedal (hold its power button until OFF, leave it 2–3 minutes, then boot and
  reconnect USB) and run the tool again.

## Files

```
annblackbox-custom-firmware-installer.py    the whole tool (stdlib only; embeds fallback copies of both firmwares)
firmware/ANNBLACKBOX_custom.fwsc            custom firmware  (sha256 bfb5b267…2b36)
firmware/BlackBox_FACTORY_V20.fwsc          factory V20      (sha256 c0fef191…cb08)
logs/                                       one persistent log per run (created at run time)
windows/M-UPGRADE-NTFS-BLACKBOX.exe.pristine   untouched vendor binary (sha256 9444eb6e…97b8)
windows/official-updater/                   the official updater (Qt) — working copy
LICENSE
```

Presets are **not touched** by a flash — only the firmware is replaced.

## Disclaimer

This is **not an official M-VAVE product** and it is **not affiliated with, authorized,
endorsed or supported by M-VAVE, Sinco, or any related brand or company**. All product
names, trademarks and brands belong to their respective owners. This is an independent,
community-made tool published by **entitymar**.

It is provided **“as is”, without warranty of any kind**, express or implied. You use it
**entirely at your own risk and precaution**:

* flashing firmware can damage or brick your pedal if something goes wrong (power loss,
  unplugging during the write, third-party software, etc.);
* modifying or flashing firmware may **void your warranty**;
* the author is **not responsible** for any damage to your device, data loss, or any
  other consequence of using this tool.

By downloading, running or using this tool you accept these terms.

## Credits

Created by **entitymar** — <https://entitymar.pages.dev/>
Repo: <https://github.com/entitymar/annblackbox-custom-firmware-installer>

The OTA engine is the original vendor updater (M-UPGRADE-NTFS-BLACKBOX, Sinco); this
project only patches its embedded firmware and two guard branches at run time.
