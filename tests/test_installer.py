"""Offline protocol and integration checks. No physical USB device is touched."""
import ast
import importlib.util
import io
import json
import os
import queue
import random
import struct
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "annblackbox-custom-firmware-installer.py"
sys.path.insert(0, str(MODULE.parent))
import blackbox.core as b
import blackbox.setup as setup
import blackbox.resources as resources
STOCK_SYSEX = bytes.fromhex(
    "F0 00 32 45 58 01 00 00 21 6C 42 0D 5B 26 68 1B 3C 5F 60 48 01 03 "
    "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 02 F7"
)


class FastControl(b.Control):
    def pause(self, seconds):
        self.check()


class FakeMidiConnection:
    def __init__(self, env, ota=False):
        self.env = env
        self.ota = ota
        self.requests = []
        self.sent = []
        self.closed = False

    def send_sysex(self, raw):
        self.env.trace.append("enter-ota-midi" if self.ota else "enter-preparation")
        assert raw == b.ENTER_OTA
        if self.env.reject_midi:
            self.requests = [(0, 0xD0000083, 0)]
        elif self.ota:
            self.requests = [(0, 0xC1000000, 0), (0, 1024, 4096), (0, 0xF0000000, 0)]
        else:
            self.requests = [(0, 0, 512), (0, 0xE0000000, 0)]

    def receive(self, timeout):
        flash, address, length = self.requests.pop(0)
        body = bytes([flash])+address.to_bytes(4, "little")+length.to_bytes(3, "little")
        return b.midi_frame(0x30, body)

    def send(self, raw):
        command, body = b.parse_midi_frame(raw)
        self.sent.append(raw)
        address = int.from_bytes(body[1:5], "little")
        if address == 0xE0000000 and not self.ota:
            self.env.prepared = True
        if address == 0xF0000000:
            self.env.finished = True

    def send_terminal(self, raw):
        b.terminal_sysex(raw)
        self.close()
        self.env.trace.append("isolated-terminal-ack")
        self.send(raw)

    def close(self):
        self.closed = True


class FakeMidi:
    def __init__(self, env):
        self.env = env

    def ports(self):
        return ["USB-Midi"], ["USB-Midi"]

    def discover(self, selected=None, normal_only=False):
        self.env.trace.append("discover-normal" if normal_only else "discover")
        if self.env.prepared and not self.env.finished and self.env.legacy:
            identity = ("OTA-BlackBox", 21)
        else:
            identity = ("BlackBox", self.env.final_version if self.env.finished else 20)
        if self.env.finished and self.env.cancel_before_commit:
            raise AssertionError("must not reboot-verify a cancelled flash")
        c = FakeMidiConnection(self.env, identity[0] != "BlackBox")
        self.env.midi_connections.append(c)
        return c, identity, ("USB-Midi", "USB-Midi")


class FakeHidDevice:
    def __init__(self, env):
        self.env = env
        self.decoder = b.HidDecoder()
        self.responses = []
        self.closed = False
        self.reverse_challenge = bytes(range(16))

    def open_path(self, path):
        assert path == b"blackbox-loader"
        self.env.trace.append("open-hid")

    def push(self, kind, data=b"", message=0):
        self.responses.extend(b.hid_reports(kind, message, data))

    def write(self, raw):
        for kind, message, data in self.decoder.feed(raw):
            if kind == 0:
                self.push(1)
            elif data == bytes.fromhex("FEDCBAC00600020001EF"):
                self.env.trace.append("auth-reset")
            elif len(data) == 17 and data[0] == 0:
                self.push(2, b"\x01"+b.auth_response(data[1:]))
            elif data == b"\x02pass":
                self.push(2, b"\0"+self.reverse_challenge)
            elif len(data) == 17 and data[0] == 1:
                assert data[1:] == b.auth_response(self.reverse_challenge)
                self.push(2, b"\x02pass")
            else:
                frame = b.parse_rcsp(data)
                self.env.trace.append(f"rcsp-{frame.opcode:02x}-{'cmd' if frame.command else 'reply'}")
                if frame.command:
                    answer = b""
                    if frame.opcode == 0xE1:
                        answer = struct.pack(">IH", 0, 512)
                    elif frame.opcode == 0xE2:
                        assert frame.data == self.env.firmware.payload[:512]
                        answer = b"\x01" if self.env.reject_hid else b"\0"
                        if self.env.cancel_before_commit:
                            self.env.control.request_cancel()
                    elif frame.opcode == 0xE3:
                        assert self.env.control.critical
                        self.env.cancel_denied = not self.env.control.request_cancel()
                    self.push(2, b.rcsp_response(frame.opcode, frame.sequence, answer))
                    if frame.opcode == 0xE3:
                        self.push(2, b.rcsp_command(0xE5, 8, struct.pack(">IH", 1024, 4096)))
                elif frame.opcode == 0xE5:
                    assert frame.data == self.env.firmware.payload[1024:5120]
                    self.push(2, b.rcsp_command(0xE8, 9))
                elif frame.opcode == 0xE8:
                    self.env.finished = True
        return len(raw)

    def read(self, length, timeout):
        if not self.responses:
            raise AssertionError("Unexpected read without response")
        return list(self.responses.pop(0))

    def close(self):
        self.closed = True


class FakeHid:
    def __init__(self, env):
        self.env = env

    def enumerate(self, vid, pid):
        assert (vid, pid) == (b.HID_VID, b.HID_PID)
        if self.env.prepared and not self.env.finished and not self.env.legacy:
            return [{"path": b"blackbox-loader"}]
        return []

    def device(self):
        d = FakeHidDevice(self.env)
        self.env.hid_devices.append(d)
        return d


class Environment:
    def __init__(self, legacy=False, reject_midi=False, reject_hid=False, cancel_before_commit=False):
        self.legacy = legacy
        self.reject_midi = reject_midi
        self.reject_hid = reject_hid
        self.cancel_before_commit = cancel_before_commit
        self.prepared = self.finished = self.cancel_denied = False
        self.final_version = 20
        self.trace = []
        self.midi_connections = []
        self.hid_devices = []
        self.temp = tempfile.TemporaryDirectory()
        self.control = FastControl(log_dir=Path(self.temp.name))
        self.firmware = b.Firmware.parse(b.factory_bytes())
        self.installer = b.Installer(self.control, FakeMidi(self), FakeHid(self))

    def close(self):
        self.control.close()
        self.temp.cleanup()


class ProtocolTests(unittest.TestCase):
    def test_factory_exact(self):
        fw = b.Firmware.parse(b.factory_bytes())
        self.assertEqual(fw.sha256, b.STOCK_SHA)
        self.assertEqual((fw.name, fw.version, fw.slots, len(fw.payload)), ("BlackBox", 20, 20, 700416))

    def test_reference_identity(self):
        self.assertEqual(b.parse_identity(b.unpack7(STOCK_SYSEX[1:-1])), ("BlackBox", 20))
        self.assertEqual(b.IDENTITY_QUERY, b"\xF0"+b.pack7(b.midi_frame(0x11))+b"\xF7")

    def test_legacy_identity(self):
        raw = b.midi_frame(0x11, b"OTA-BlackBox_020\0")
        self.assertEqual(b.parse_identity(raw), ("OTA-BlackBox", 20))
        # The real bootloader reports a lowercase prefix and its own version.
        raw = b.midi_frame(0x11, b"ota-BlackBox_021\0")
        self.assertEqual(b.parse_identity(raw), ("OTA-BlackBox", 21))

    def test_7bit_codec(self):
        rng = random.Random(8)
        for size in range(256):
            raw = rng.randbytes(size)
            packed = b.pack7(raw)
            self.assertTrue(all(x < 128 for x in packed))
            self.assertEqual(b.unpack7(packed), raw)

    def test_midi_corruption(self):
        raw = bytearray(b.midi_frame(0x30, b"abcdefgh"))
        raw[-1] ^= 1
        with self.assertRaises(b.InstallerError):
            b.parse_midi_frame(bytes(raw))

    def test_authentication_known_vector(self):
        self.assertEqual(b.auth_response(bytes(range(16))).hex(), "e5807e887bde07630b4d0e715b3e8991")

    def test_wrong_challenge_size(self):
        with self.assertRaises(b.InstallerError):
            b.auth_response(b"wrong")

    def test_rcsp_known_bytes(self):
        self.assertEqual(b.rcsp_command(0xE5, 7, bytes.fromhex("000001000020")),
                         bytes.fromhex("FEDCBAC0E5000707000001000020EF"))
        self.assertEqual(b.rcsp_response(0xE5, 7, b"abc"), bytes.fromhex("FEDCBA00E500050007616263EF"))

    def test_rcsp_corrupt_length(self):
        data = b.rcsp_command(0xE5, 7, b"abc")[:-2]+b"\xEF"
        with self.assertRaises(b.InstallerError):
            b.parse_rcsp(data)

    def test_hid_fragmentation(self):
        rng = random.Random(32)
        for size in (0, 1, 55, 56, 57, 63, 64, 65, 127, 512, 1024, 65534):
            data = rng.randbytes(size)
            reports = b.hid_reports(2, 0, data)
            decoder = b.HidDecoder()
            result = []
            for index, report in enumerate(reports):
                result += decoder.feed(b"\0"+report if index % 2 else report)
            self.assertEqual(result, [(2, 0, data)])

    def test_hid_wrong_trailer(self):
        report = bytearray(b.hid_reports(2, 0, b"abc")[0])
        report[10] = 0
        with self.assertRaises(b.InstallerError):
            b.HidDecoder().feed(bytes(report))

    def test_bad_firmware(self):
        for position in (0, 80, 5000, 690000, 700400):
            data = bytearray(b.factory_bytes())
            data[position] ^= 2
            with self.subTest(position=position), self.assertRaises(b.InstallerError):
                b.Firmware.parse(bytes(data))

    def test_non_v20_file(self):
        data = bytearray(b.factory_bytes())
        data[11*48+47] += 1
        with self.assertRaisesRegex(b.InstallerError, "V21"):
            b.Firmware.parse(bytes(data))

    def test_truncated_firmware(self):
        with self.assertRaises(b.InstallerError):
            b.Firmware.parse(b.factory_bytes()[:-1])

    def test_out_of_bounds_request(self):
        firmware = b.Firmware.parse(b.factory_bytes())
        for offset, size in ((0, 0), (-1, 1), (700410, 20)):
            with self.subTest(offset=offset), self.assertRaises(b.InstallerError):
                firmware.slice(offset, size)


class IntegrationTests(unittest.TestCase):
    def test_hid_install_and_verify(self):
        env = Environment()
        try:
            env.installer.run(env.firmware, "original")
            self.assertTrue(env.finished)
            self.assertTrue(env.cancel_denied)
            self.assertIn("rcsp-e8-reply", env.trace)
            self.assertFalse(env.control.critical)
            self.assertTrue(all(c.closed for c in env.midi_connections+env.hid_devices))
            self.assertIn("confirmed", env.control.log_path.read_text())
        finally:
            env.close()

    def test_legacy_midi_install(self):
        env = Environment(legacy=True)
        try:
            env.installer.run(env.firmware, "custom")
            self.assertTrue(env.finished)
            self.assertIn("enter-ota-midi", env.trace)
            self.assertFalse(env.hid_devices)
            # Only the F completion may use the isolated ACK; the C-state
            # request before it must go through the still-open main ports.
            self.assertEqual(env.trace.count("isolated-terminal-ack"), 1)
        finally:
            env.close()

    def test_same_version_device_rejection(self):
        env = Environment(reject_midi=True)
        try:
            with self.assertRaisesRegex(b.InstallerError, "identical"):
                env.installer.run(env.firmware, "original")
            self.assertFalse(env.installer.wrote)
            self.assertFalse(env.hid_devices)
            self.assertFalse(env.control.critical)
        finally:
            env.close()

    def test_cfg_rejection_no_commit(self):
        env = Environment(reject_hid=True)
        try:
            with self.assertRaisesRegex(b.InstallerError, "configuration"):
                env.installer.run(env.firmware, "custom")
            self.assertNotIn("rcsp-e3-cmd", env.trace)
            self.assertFalse(env.installer.wrote)
        finally:
            env.close()

    def test_cancel_before_e3(self):
        env = Environment(cancel_before_commit=True)
        try:
            with self.assertRaises(b.Cancelled):
                env.installer.run(env.firmware, "custom")
            self.assertNotIn("rcsp-e3-cmd", env.trace)
            self.assertFalse(env.installer.wrote)
            self.assertTrue(all(c.closed for c in env.hid_devices))
        finally:
            env.close()

    def test_cancel_before_usb(self):
        env = Environment()
        try:
            env.control.request_cancel()
            with self.assertRaises(b.Cancelled):
                env.installer.run(env.firmware, "original")
            self.assertFalse(env.trace)
        finally:
            env.close()


class BootstrapTests(unittest.TestCase):
    def test_newer_and_32bit_launchers_need_private_runtime(self):
        with patch.object(setup.sys, "version_info", (3, 14, 0)), patch.object(setup.platform, "python_implementation", return_value="CPython"):
            self.assertFalse(setup.compatible_python())
        with patch.object(setup.sys, "version_info", (3, 12, 0)), patch.object(setup.struct, "calcsize", return_value=4):
            self.assertFalse(setup.compatible_python())

    def test_windows_32bit_launcher_uses_64bit_host(self):
        with patch.object(setup.platform, "system", return_value="Windows"), patch.object(setup.platform, "machine", return_value="x86"), \
             patch.dict(os.environ, {"PROCESSOR_ARCHITEW6432": "AMD64", "PROCESSOR_ARCHITECTURE": "x86"}):
            self.assertEqual(setup.managed_platform(), ("Windows", "x86_64", "none"))

    def test_true_32bit_host_reports_unsupported_runtime(self):
        with patch.object(setup.platform, "system", return_value="Windows"), patch.object(setup.platform, "machine", return_value="x86"), \
             patch.dict(os.environ, {"PROCESSOR_ARCHITECTURE": "x86"}, clear=True):
            with self.assertRaisesRegex(setup.InstallerError, "No automatic desktop runtime"):
                setup.managed_platform()

    def test_tampered_download_is_not_published(self):
        target = ("Linux", "x86_64", "gnu")
        tag, expected = setup.UV_WHEELS[target[:2]]
        metadata = {"urls": [{"filename": f"uv-{setup.UV_VERSION}-py3-none-{tag}.whl",
                             "digests": {"sha256": expected}, "size": 6,
                             "url": "https://files.pythonhosted.org/packages/test/runtime.whl"}]}
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(setup.urllib.request, "urlopen", side_effect=[io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(b"broken")]):
            root = Path(temp)
            with self.assertRaisesRegex(setup.InstallerError, "SHA-256"):
                setup.download_runtime_tool(root, target, queue.Queue(), threading.Event())
            self.assertFalse(list(root.rglob("uv")))
            self.assertFalse(list(root.rglob("runtime.whl")))

    def test_verified_download_and_offline_reuse(self):
        target = ("Linux", "x86_64", "gnu")
        wheel = io.BytesIO()
        executable = b"verified test executable; never run"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(f"uv-{setup.UV_VERSION}.data/scripts/uv", executable)
            archive.writestr("../../outside.txt", b"must not extract")
        data = wheel.getvalue()
        tag = setup.UV_WHEELS[target[:2]][0]
        expected = setup.digest(data)
        metadata = {"urls": [{"filename": f"uv-{setup.UV_VERSION}-py3-none-{tag}.whl",
                             "digests": {"sha256": expected}, "size": len(data),
                             "url": "https://files.pythonhosted.org/packages/test/runtime.whl"}]}
        with tempfile.TemporaryDirectory() as temp, patch.dict(setup.UV_WHEELS, {target[:2]: (tag, expected)}), \
             patch.object(setup.urllib.request, "urlopen", side_effect=[io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(data)]) as network:
            root = Path(temp)
            tool = setup.download_runtime_tool(root, target, queue.Queue(), threading.Event())
            self.assertEqual(tool.read_bytes(), executable)
            self.assertFalse(list(root.rglob("outside.txt")))
            self.assertEqual(setup.download_runtime_tool(root, target, queue.Queue(), threading.Event()), tool)
            self.assertEqual(network.call_count, 2)

    def test_cached_runtime_launches_without_installing(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(setup, "compatible_python", return_value=False), \
             patch.object(setup, "managed_platform", return_value=("Linux", "x86_64", "gnu")), \
             patch.object(setup, "data_root", return_value=Path(temp)), patch.object(setup, "runtime_ready", return_value=True), \
             patch.object(setup, "launch_runtime", side_effect=SystemExit(0)) as launch, \
             patch.object(setup, "download_runtime_tool") as download:
            with self.assertRaises(SystemExit) as result:
                setup.ensure_dependencies()
            self.assertEqual(result.exception.code, 0)
            self.assertIn("runtime-managed-3.12", str(launch.call_args.args[0]))
            download.assert_not_called()

    def test_cancelled_setup_starts_no_process(self):
        stop = threading.Event()
        stop.set()
        with patch.object(setup.subprocess, "Popen") as start:
            with self.assertRaisesRegex(setup.InstallerError, "cancelled"):
                setup.setup_process(["never-execute"], io.StringIO(), stop, {})
            start.assert_not_called()


class ResourceAndTerminalTests(unittest.TestCase):
    def test_external_factory_firmware_is_exact(self):
        path = resources.application_dir() / "firmware/BlackBox_FACTORY_V20.fwsc"
        self.assertEqual(resources.factory_bytes(), path.read_bytes())
        self.assertEqual(resources.digest(path.read_bytes()), b.STOCK_SHA)

    def test_missing_external_firmware_has_no_embedded_fallback(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(resources, "APP_DIR", Path(temp)):
            with self.assertRaisesRegex(b.InstallerError, "missing or unreadable"):
                resources.factory_bytes()

    def test_corrupted_external_firmware_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(resources, "APP_DIR", Path(temp)):
            path = Path(temp) / "firmware/BlackBox_FACTORY_V20.fwsc"
            path.parent.mkdir()
            path.write_bytes(b"damaged firmware")
            with self.assertRaisesRegex(b.InstallerError, "SHA-256"):
                resources.factory_bytes()

    def test_logs_stay_in_program_folder(self):
        fw = b.Firmware.parse(b.factory_bytes())
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "program"
            elsewhere = Path(temp) / "elsewhere"
            elsewhere.mkdir()
            with patch.object(resources, "APP_DIR", folder), patch.dict(os.environ, {"LOCALAPPDATA": str(elsewhere)}):
                try:
                    os.chdir(elsewhere)
                    control = b.Control()
                    control.open_log("original", fw)
                    self.assertEqual(control.log_path.parent, folder / "logs")
                    control.log("test entry")
                    control.close()
                    self.assertIn("test entry", control.log_path.read_text())
                    self.assertFalse((elsewhere / "logs").exists())
                finally:
                    os.chdir(previous)

    def test_native_files_and_helper_flag_are_verified(self):
        path = resources.verify_native_files()
        self.assertIn(b"--midi-terminal-ack", path.read_bytes())
        self.assertEqual(resources.digest(path.read_bytes()), "cbda7a95e506cdeefbe39e9718106586147f2ee9857a83e0509644e349591e3b")

    def test_nonterminal_packet_cannot_reach_helper(self):
        with self.assertRaisesRegex(b.InstallerError, "only the E/F"):
            b.terminal_sysex(b.upload_response(0, 1024, b"success\0"))

    def test_recorded_preparation_reaches_isolated_ack(self):
        requests = json.loads((Path(__file__).parent / "fixtures/preparation_requests.json").read_text())
        trace = []
        replies = []
        class Connection:
            def send_sysex(self, raw):
                self.assert_packet = raw
            def receive(self, timeout):
                flash, address, length = requests.pop(0)
                return b.midi_frame(0x30, bytes([flash])+address.to_bytes(4,"little")+length.to_bytes(3,"little"))
            def send(self, raw):
                _, body = b.parse_midi_frame(raw)
                replies.append(body)
            def send_terminal(self, raw):
                trace.extend(["close-input", "close-output", "isolated-ack"])
                b.terminal_sysex(raw)
        fw = b.Firmware.parse(b.factory_bytes())
        installer = b.Installer(FastControl(), FakeMidi(SimpleNamespace()), FakeHid(SimpleNamespace()))
        installer.prepare_midi(Connection(), fw)
        self.assertFalse(requests)
        self.assertEqual(len(replies), 48)
        for body in replies:
            address = int.from_bytes(body[1:5], "little")
            length = int.from_bytes(body[5:8], "little")
            self.assertEqual(body[8:], fw.slice(address,length))
        self.assertEqual(trace, ["close-input", "close-output", "isolated-ack"])
        self.assertFalse(installer.wrote)

    def test_windows_helper_runs_after_both_ports_close_and_never_retries(self):
        for exit_code in (0, 67):
            with self.subTest(exit_code=exit_code):
                trace=[]
                class Port:
                    def __init__(self, name):
                        self.name = name
                    def close_port(self):
                        trace.append(self.name+"-closed")
                    def delete(self):
                        trace.append(self.name+"-deleted")
                    def get_ports(self):
                        return ["GS 0", "USB-Midi 1"]
                connection=object.__new__(b.MidiConnection)
                connection.control=FastControl()
                connection.closed=False
                connection.input, connection.output=Port("input"), Port("output")
                connection.output_name="USB-Midi 1"
                connection.api=SimpleNamespace(API_WINDOWS_MM=1, MidiOut=lambda **kwargs: Port("enumerator"))
                helper=resources.native_helper_path()
                packet=b.upload_response(0, 0xE0000000, b"success\0")
                def spawn(command, **kwargs):
                    self.assertIn("input-closed",trace)
                    self.assertIn("output-closed",trace)
                    self.assertEqual(command,[str(helper),"--midi-terminal-ack","1",b.terminal_sysex(packet).hex()])
                    trace.append("helper-started")
                    return SimpleNamespace(returncode=exit_code, poll=lambda: exit_code)
                with patch.object(b.sys,"platform","win32"), patch.object(b,"verify_native_files",return_value=helper), \
                     patch.object(b,"native_helper_environment",return_value={}), \
                     patch.object(b.subprocess,"CREATE_NO_WINDOW",0x08000000,create=True), \
                     patch.object(b.subprocess,"Popen",side_effect=spawn) as process:
                    if exit_code:
                        with self.assertRaisesRegex(b.InstallerError,"exit 67"):
                            connection.send_terminal(packet)
                    else:
                        connection.send_terminal(packet)
                    self.assertEqual(process.call_count,1)
                    connection.close()
                    self.assertEqual(trace.count("input-closed"),1)

    def test_linux_mac_terminal_child_opens_output_only(self):
        trace=[]
        packet=b.terminal_sysex(b.upload_response(0, 0xF0000000, b"success\0"))
        class Output:
            def get_ports(self):return ["USB-Midi"]
            def open_port(self,index):trace.append(("open-output",index))
            def send_message(self,data):trace.append(("send",bytes(data)))
            def close_port(self):trace.append("close-output")
            def delete(self):trace.append("delete-output")
        api=SimpleNamespace(MidiOut=lambda **kwargs: Output())
        with patch.object(b.sys,"platform","linux"), patch.object(b.time,"sleep"):
            self.assertEqual(b.terminal_ack_child("USB-Midi",packet.hex(),api),0)
        self.assertEqual(trace,[("open-output",0),("send",packet),"close-output","delete-output"])

    def test_native_preflight_failure_stops_before_midi(self):
        env=Environment()
        try:
            with patch.object(b.sys,"platform","win32"), \
                 patch.object(b,"native_helper_preflight",side_effect=b.InstallerError("DLL unavailable")):
                with self.assertRaisesRegex(b.InstallerError,"DLL unavailable"):
                    env.installer.run(env.firmware,"original")
            self.assertFalse(env.trace)
            self.assertFalse(env.installer.wrote)
        finally:
            env.close()

    def test_failed_handle_release_prevents_terminal_helper(self):
        connection=object.__new__(b.MidiConnection)
        connection.control=FastControl()
        connection.closed=False
        def fail_close():
            raise OSError("driver did not close")
        connection.input=SimpleNamespace(close_port=fail_close,delete=lambda:None)
        connection.output=SimpleNamespace(close_port=lambda:None,delete=lambda:None)
        with patch.object(b.subprocess,"Popen") as process:
            with self.assertRaisesRegex(b.TransportLost,"release the main MIDI handles"):
                connection.send_terminal(b.upload_response(0,0xE0000000,b"success\0"))
            process.assert_not_called()

    def test_legacy_verification_only_is_not_success(self):
        trace=[]
        connection=SimpleNamespace(send_sysex=lambda raw:None,
            receive=lambda timeout:b.midi_frame(0x30,b"\0"+int(0xE0000000).to_bytes(4,"little")+b"\x08\0\0"),
            send_terminal=lambda raw:trace.append(b.terminal_sysex(raw)))
        installer=b.Installer(FastControl(),FakeMidi(SimpleNamespace()),FakeHid(SimpleNamespace()))
        with self.assertRaisesRegex(b.InstallerError,"without firmware-write completion"):
            installer.transfer_midi(connection,b.Firmware.parse(b.factory_bytes()))
        self.assertEqual(len(trace),1)
        self.assertFalse(installer.wrote)


if __name__ == "__main__":
    unittest.main()
