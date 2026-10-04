"""Minimal desktop window and command line entry points."""
from __future__ import annotations
import argparse
import json
import platform
import queue
import sys
import threading
from pathlib import Path
from .core import *
from .setup import ensure_dependencies, compatible_python, dependencies_ready

def run_ui(demo=False):
    from PySide6 import QtCore, QtWidgets, QtGui

    class Window(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(TITLE)
            self.setMinimumSize(540, 420)
            self.resize(570, 430)
            self.events = queue.Queue()
            self.worker = None
            self.control = None
            self.selected = None
            self.demo = demo
            self.setStyleSheet("""
                QWidget { background:#fafafa; color:#171717; font-size:14px; }
                QLabel#heading {font-size:24px; font-weight:600;}
                QLabel#credit {color:#777; font-size:12px;}
                QPushButton {background:white; border:1px solid #d8d8d8; border-radius:7px; padding:13px;}
                QPushButton:hover {background:#eeeeee;}
                QPushButton:disabled {color:#999; background:#f1f1f1;}
                QPushButton#install {background:#171717; color:white; border-color:#171717;}
                QPushButton#install:hover {background:#333;}
                QProgressBar {border:0; background:#e5e5e5; border-radius:3px; height:6px;}
                QProgressBar::chunk {background:#171717; border-radius:3px;}
                QToolButton {border:0; color:#666; padding:4px;}
            """)
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(30, 25, 30, 20)
            layout.setSpacing(13)
            head = QtWidgets.QLabel("ANN BlackBox")
            head.setObjectName("heading")
            layout.addWidget(head)
            layout.addWidget(QtWidgets.QLabel("Custom Firmware Installer · M-VAVE V20"))
            layout.addSpacing(8)
            self.install = QtWidgets.QPushButton("Install custom firmware")
            self.install.setObjectName("install")
            self.restore = QtWidgets.QPushButton("Return to V20 original")
            self.cancel = QtWidgets.QPushButton("Cancel")
            self.install.clicked.connect(self.choose_custom)
            self.restore.clicked.connect(self.restore_original)
            self.cancel.clicked.connect(self.cancel_clicked)
            for b in (self.install, self.restore, self.cancel):
                layout.addWidget(b)
            self.bar = QtWidgets.QProgressBar()
            self.bar.setTextVisible(False)
            self.bar.setRange(0, 100)
            layout.addWidget(self.bar)
            self.status = QtWidgets.QLabel("Connect BlackBox by USB.")
            self.status.setWordWrap(True)
            self.status.setMinimumHeight(42)
            layout.addWidget(self.status)
            footer = QtWidgets.QHBoxLayout()
            connection = QtWidgets.QToolButton()
            connection.setText("USB connection")
            connection.clicked.connect(self.configure_ports)
            self.connection_button = connection
            footer.addWidget(connection)
            footer.addStretch()
            credit = QtWidgets.QLabel("created by entitymar")
            credit.setObjectName("credit")
            footer.addWidget(credit)
            layout.addLayout(footer)
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self.pump)
            self.timer.start(50)

        def busy(self):
            return self.worker is not None and self.worker.is_alive()

        def configure_ports(self):
            if self.busy():
                return
            try:
                midi = MidiDevices(Control())
                ins, outs = midi.ports()
                dialog = QtWidgets.QDialog(self)
                dialog.setWindowTitle("USB connection")
                form = QtWidgets.QFormLayout(dialog)
                auto = QtWidgets.QCheckBox("Automatic detection")
                auto.setChecked(self.selected is None)
                form.addRow(auto)
                input_box, output_box = QtWidgets.QComboBox(), QtWidgets.QComboBox()
                input_box.addItems(ins)
                output_box.addItems(outs)
                form.addRow("MIDI input", input_box)
                form.addRow("MIDI output", output_box)
                if self.selected:
                    input_box.setCurrentText(self.selected[0])
                    output_box.setCurrentText(self.selected[1])
                buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
                buttons.accepted.connect(dialog.accept)
                buttons.rejected.connect(dialog.reject)
                form.addRow(buttons)
                if dialog.exec() == QtWidgets.QDialog.Accepted:
                    if not auto.isChecked() and (not ins or not outs):
                        raise InstallerError("No MIDI ports are available.")
                    self.selected = None if auto.isChecked() else (input_box.currentText(), output_box.currentText())
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, TITLE, str(exc))

        def choose_custom(self):
            if self.busy():
                return
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select custom BlackBox V20 firmware", "", "BlackBox firmware (*.fwsc)")
            if not path:
                return
            try:
                p = Path(path)
                if p.stat().st_size > 16*1024*1024:
                    raise InstallerError("Firmware file is too large.")
                firmware = Firmware.parse(p.read_bytes())
                if firmware.sha256 == STOCK_SHA:
                    raise InstallerError("This is the original V20. Use Return to V20 original, or select a custom FWSC.")
                note = ""
                if firmware.sha256 == BRIDGE_SHA:
                    note = "\nThis is the experimental bridge from factory hardreset: one internal tag changes to 021. It adds no custom features."
                message = f"Install {p.name}?\n\nBlackBox_020 · {len(firmware.payload):,} bytes\nPresets may be affected. Keep USB connected until verification finishes.{note}"
                if QtWidgets.QMessageBox.question(self, TITLE, message) == QtWidgets.QMessageBox.Yes:
                    self.start(firmware, "custom")
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, TITLE, str(exc))

        def restore_original(self):
            if self.busy():
                return
            try:
                firmware = Firmware.parse(factory_bytes())
                if QtWidgets.QMessageBox.question(self, TITLE, "Restore the exact original BlackBox V20 firmware?\n\nKeep USB connected until verification finishes. This does not restore a backup of your presets.") == QtWidgets.QMessageBox.Yes:
                    self.start(firmware, "original")
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, TITLE, str(exc))

        def start(self, firmware, mode):
            self.control = Control(lambda kind, value: self.events.put((kind, value)))
            self.bar.setValue(0)
            for b in (self.install, self.restore, self.connection_button):
                b.setEnabled(False)
            self.status.setText("Connecting to BlackBox…")
            def task():
                try:
                    if self.demo:
                        self.control.status("Preview mode. USB writes are disabled.")
                        self.control.pause(1)
                    else:
                        Installer(self.control).run(firmware, mode, self.selected)
                    self.events.put(("done", None))
                except Cancelled as exc:
                    self.events.put(("cancelled", str(exc)))
                except Exception as exc:
                    detail = str(exc)
                    if self.control.log_path:
                        try:
                            relative = self.control.log_path.relative_to(application_dir())
                        except ValueError:
                            relative = self.control.log_path
                        detail += "\n\nLog: " + str(relative)
                    self.events.put(("error", detail))
            self.worker = threading.Thread(target=task, daemon=False)
            self.worker.start()

        def cancel_clicked(self):
            if self.busy():
                if self.control.request_cancel():
                    self.status.setText("Cancelling before firmware writing…")
            else:
                self.close()

        def pump(self):
            while not self.events.empty():
                kind, value = self.events.get_nowait()
                if kind == "status":
                    self.status.setText(value)
                elif kind == "progress":
                    self.bar.setValue(value)
                elif kind == "critical":
                    self.cancel.setEnabled(not value)
                elif kind in ("done", "error", "cancelled"):
                    for b in (self.install, self.restore, self.cancel, self.connection_button):
                        b.setEnabled(True)
                    if kind == "error":
                        self.status.setText("Stopped. Check the message and log.")
                        QtWidgets.QMessageBox.warning(self, TITLE, value)
                    elif kind == "cancelled":
                        self.status.setText(value)

        def closeEvent(self, event):
            if self.busy():
                if self.control.request_cancel():
                    self.status.setText("Cancelling… Wait for the operation to stop, then close.")
                event.ignore()
            else:
                event.accept()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName(TITLE)
    app.setStyle("Fusion")
    window = Window()
    if demo:
        window.status.setText("Preview mode · USB writes disabled.")
    window.show()
    return app.exec()


def main():
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--self-test", action="store_true", help="Offline checks; no dependencies or USB writes")
    parser.add_argument("--setup-test", action="store_true", help="Test automatic setup and offline checks; no USB writes")
    parser.add_argument("--check-dependencies", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--terminal-ack", nargs=2, metavar=("OUTPUT", "HEX"), help=argparse.SUPPRESS)
    parser.add_argument("--inspect", metavar="FWSC", help="Validate firmware without USB or dependency installation")
    parser.add_argument("--export-original", metavar="PATH", help="Save the separate original V20 FWSC; never writes USB")
    parser.add_argument("--demo", action="store_true", help="Preview the window with USB writes disabled")
    args = parser.parse_args()
    if args.terminal_ack:
        return terminal_ack_child(*args.terminal_ack)
    if args.check_dependencies:
        return 0 if compatible_python() and dependencies_ready() else 1
    if args.setup_test:
        ensure_dependencies()
    if args.self_test or args.setup_test:
        auth_selftest()
        stock = Firmware.parse(factory_bytes())
        print(json.dumps({"authentication": "passed", "original_v20_sha256": stock.sha256,
                          "wrapper": "BlackBox_020", "slots": stock.slots, "payload_bytes": len(stock.payload),
                          "python": platform.python_version(), "dependencies_checked": args.setup_test,
                          "usb_writes": 0}, indent=2))
        return 0
    if args.inspect:
        p = Path(args.inspect)
        if p.stat().st_size > 16*1024*1024:
            raise InstallerError("Firmware file is too large.")
        fw = Firmware.parse(p.read_bytes())
        print(json.dumps({"name": fw.name, "version": fw.version, "sha256": fw.sha256,
                          "payload_bytes": len(fw.payload), "sections": fw.sections}, indent=2))
        return 0
    if args.export_original:
        p = Path(args.export_original)
        # Explicit output only; do not silently overwrite an existing firmware.
        with p.open("xb") as f:
            f.write(factory_bytes())
        print(str(p.resolve()))
        return 0
    ensure_dependencies()
    return run_ui(args.demo)
