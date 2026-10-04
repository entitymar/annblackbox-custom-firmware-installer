# Technical notes

## Update surface

The installer uses the update interfaces already exposed by the BlackBox firmware and bootloader. Its relevant surface is the USB firmware-update chain:

1. **USB-MIDI device identity**
2. **OTA preparation over MIDI SysEx**
3. **Update-mode identity / transport selection**
4. **JL HID loader or legacy OTA-MIDI block requests**
5. **Completion acknowledgement**
6. **Normal-device identity after reboot**

It does not require filesystem mounting, WinUSB/Zadig replacement, arbitrary memory browsing or a second hidden updater window.

## FWSC handling

The stock restore image lives at `firmware/BlackBox_FACTORY_V20.fwsc` and is read from disk. Its expected SHA-256 is:

`c0fef191b860d3b20f77fc492a24986eef2d12df44bb5e38355684820e26cb08`

The container reports the V20 wrapper family (`BlackBox_020`). Custom images are parsed before a device session begins. The implementation validates the parts of the FWSC container required for safe transport and rejects malformed or incompatible inputs before flashing.

## Same-version flashing

The desktop-side same-version block is intentionally not enforced. This allows a V20 image to be presented to a V20 device. The device is still free to reject the transaction during its own validation stages.

No automatic V20-to-V21 bridge is inserted and the stock firmware is not silently modified to advertise another version.

## Windows native helper

On Windows, `native/windows/M-UPGRADE/M-UPGRADE.exe` is used only for the terminal MIDI acknowledgement path. Its native files are checked against `native/windows/M-UPGRADE/FILES.json` before use.

Expected executable SHA-256:

`cbda7a95e506cdeefbe39e9718106586147f2ee9857a83e0509644e349591e3b`

The normal M-UPGRADE graphical interface is not part of the installer flow.

## Success criteria

A transfer is not treated as complete solely because the host sent the last payload. The normal success path requires the update transaction to finish and the device to return as a normal BlackBox identity after reboot.

Unexpected disconnects before a completion indication are treated as failures. Automatic write retries are intentionally avoided.

## Logs

Runtime logs are created under `logs/` inside the extracted project directory. The distributed package contains no previous workstation/session logs.
