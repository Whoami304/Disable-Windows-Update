"""
main.py – PyQt6 dark UI for the Windows Update Disabler.
Automatically asks for Administrator rights (UAC) on launch.
"""

from __future__ import annotations

import ctypes
import html
import os
import subprocess
import sys
from datetime import datetime

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from Disable_Update import Disable_Update, Status

# ─────────────────────────────────────────────
#  Theme
# ─────────────────────────────────────────────
BG, CARD, BORDER = "#0e1014", "#161a21", "#252b36"
TEXT, MUTED = "#e6e9ef", "#8a93a6"
RED, GREEN, AMBER, BLUE = "#f05252", "#31c48d", "#f6b23c", "#4f8cff"
LOG_COLORS = {"info": MUTED, "ok": GREEN, "warn": AMBER, "err": RED}

STYLE = f"""
QWidget {{ background: {BG}; color: {TEXT}; font-family: 'Segoe UI'; font-size: 10pt; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame#card QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 18pt; font-weight: 600; }}
QLabel#subtitle, QLabel#muted {{ color: {MUTED}; }}
QLabel#section {{ color: {MUTED}; font-size: 8.5pt; font-weight: 600; letter-spacing: 1px; }}
QFrame#warn {{ background: #2a2113; border: 1px solid #5c4519; border-radius: 10px; }}
QFrame#warn QLabel {{ background: transparent; color: {AMBER}; }}
QPushButton {{
    background: #222833; border: 1px solid {BORDER}; border-radius: 9px;
    padding: 10px 18px; font-weight: 600;
}}
QPushButton:hover {{ background: #2a3140; }}
QPushButton:disabled {{ color: #555d6e; background: #1a1e26; }}
QPushButton#danger {{ background: {RED}; border: none; color: white; }}
QPushButton#danger:hover {{ background: #ff6b6b; }}
QPushButton#success {{ background: #1f9d6f; border: none; color: white; }}
QPushButton#success:hover {{ background: {GREEN}; }}
QPushButton#danger:disabled, QPushButton#success:disabled {{ background: #2a2f3a; color: #666; }}
QPushButton#small {{ padding: 6px 12px; font-weight: 500; }}
QPlainTextEdit {{
    background: #0b0d11; border: 1px solid {BORDER}; border-radius: 10px;
    padding: 8px; font-family: 'Cascadia Mono', 'Consolas'; font-size: 9pt;
}}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: #2e3542; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""


def rgba(color: str, alpha: float) -> str:
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {alpha})"


def pill(color: str) -> str:
    """Stylesheet for a rounded status pill in the given color."""
    return (f"background: {rgba(color, 0.14)}; color: {color};"
            f"border: 1px solid {rgba(color, 0.35)};"
            f"border-radius: 10px; padding: 3px 10px; font-weight: 600;")


# ─────────────────────────────────────────────
#  Elevation
# ─────────────────────────────────────────────
def relaunch_as_admin() -> bool:
    """Relaunch this program elevated via UAC. Returns True if launched."""
    if getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        params = subprocess.list2cmdline([os.path.abspath(sys.argv[0]), *sys.argv[1:]])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                             params, None, 1)
    return rc > 32


# ─────────────────────────────────────────────
#  Background worker
# ─────────────────────────────────────────────
class Worker(QThread):
    log = pyqtSignal(str, str)
    done = pyqtSignal(bool)

    def __init__(self, action: str):
        super().__init__()
        self.action = action

    def run(self):
        manager = Disable_Update(log=self.log.emit)
        try:
            ok = (manager.disable_update() if self.action == "disable"
                  else manager.enable_update())
        except Exception as e:  # never let the thread die silently
            self.log.emit("err", f"Unexpected error: {e}")
            ok = False
        self.done.emit(ok)


# ─────────────────────────────────────────────
#  Main window
# ─────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.manager = Disable_Update()
        self.worker: Worker | None = None
        self.is_admin = self.manager.if_admin()

        self.setWindowTitle("Windows Update Control")
        self.setMinimumSize(640, 700)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)

        # Header
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Windows Update Control", objectName="title")
        subtitle = QLabel("Fully disable or restore Windows Update", objectName="subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()
        self.badge = QLabel()
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.badge)
        layout.addLayout(header)

        # Admin warning
        if not self.is_admin:
            warn = QFrame(objectName="warn")
            wl = QHBoxLayout(warn)
            wl.setContentsMargins(14, 10, 10, 10)
            wl.addWidget(QLabel("⚠  Not running as Administrator — changes are disabled."))
            wl.addStretch()
            btn = QPushButton("Restart as Admin", objectName="small")
            btn.clicked.connect(self.restart_admin)
            wl.addWidget(btn)
            layout.addWidget(warn)

        # Services card
        card = QFrame(objectName="card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 14, 18, 14)
        cl.addWidget(QLabel("SERVICES", objectName="section"))
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(10)
        self.grid.setColumnStretch(1, 1)
        cl.addLayout(self.grid)
        self.rows: dict[str, tuple[QLabel, QLabel, QLabel]] = {}
        for i, svc in enumerate(self.manager.get_status().services):
            dot = QLabel("●")
            name = QLabel(f"{svc.display}  <span style='color:{MUTED}'>{svc.name}</span>")
            start = QLabel()
            start.setAlignment(Qt.AlignmentFlag.AlignCenter)
            start.setMinimumWidth(150)
            state = QLabel(objectName="muted")
            state.setMinimumWidth(70)
            for col, w in enumerate((dot, name, start, state)):
                self.grid.addWidget(w, i, col)
            self.rows[svc.name] = (dot, start, state)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {BORDER};")
        cl.addSpacing(4)
        cl.addWidget(sep)
        cl.addSpacing(4)
        policy_row = QHBoxLayout()
        policy_row.addWidget(QLabel("Group Policy block"))
        policy_row.addStretch()
        self.policy_label = QLabel()
        policy_row.addWidget(self.policy_label)
        cl.addLayout(policy_row)
        layout.addWidget(card)

        # Action buttons
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.btn_disable = QPushButton("Disable Updates", objectName="danger")
        self.btn_enable = QPushButton("Enable Updates", objectName="success")
        self.btn_refresh = QPushButton("Refresh")
        self.btn_disable.clicked.connect(lambda: self.run_action("disable"))
        self.btn_enable.clicked.connect(lambda: self.run_action("enable"))
        self.btn_refresh.clicked.connect(self.refresh)
        for b in (self.btn_disable, self.btn_enable):
            b.setMinimumHeight(44)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            actions.addWidget(b, 2)
        self.btn_refresh.setMinimumHeight(44)
        actions.addWidget(self.btn_refresh, 1)
        layout.addLayout(actions)

        # Log
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("ACTIVITY", objectName="section"))
        log_header.addStretch()
        self.btn_reboot = QPushButton("Restart PC", objectName="small")
        self.btn_reboot.clicked.connect(self.reboot)
        log_header.addWidget(self.btn_reboot)
        layout.addLayout(log_header)
        self.log_view = QPlainTextEdit(readOnly=True)
        layout.addWidget(self.log_view, 1)

        self.set_busy(False)
        self.refresh()
        self.add_log("info", "Ready." if self.is_admin else
                     "Read-only mode. Restart as Administrator to make changes.")

    # ── helpers ──
    def add_log(self, level: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        color = LOG_COLORS.get(level, TEXT)
        self.log_view.appendHtml(
            f"<span style='color:#5b6375'>{ts}</span>&nbsp;&nbsp;"
            f"<span style='color:{color}'>{html.escape(msg)}</span>")

    def set_busy(self, busy: bool):
        can_act = self.is_admin and not busy
        self.btn_disable.setEnabled(can_act)
        self.btn_enable.setEnabled(can_act)
        self.btn_refresh.setEnabled(not busy)
        self.btn_reboot.setEnabled(can_act)
        self.btn_disable.setText("Working…" if busy else "Disable Updates")

    def refresh(self):
        status: Status = self.manager.get_status()
        overall = status.overall
        color = {"Disabled": RED, "Enabled": GREEN}.get(overall, AMBER)
        self.badge.setText(f"  {overall.upper()}  ")
        self.badge.setStyleSheet(pill(color) + "font-size: 10pt; padding: 6px 14px;")

        for svc in status.services:
            dot, start, state = self.rows[svc.name]
            if svc.start is None:
                c = MUTED
            elif svc.disabled:
                c = RED
            else:
                c = GREEN
            dot.setStyleSheet(f"color: {c}; font-size: 12pt;")
            start.setText(svc.start_label)
            start.setStyleSheet(pill(c))
            state.setText(svc.state_label)

        pc = RED if status.policy_applied else GREEN
        self.policy_label.setText("Applied" if status.policy_applied else "Not applied")
        self.policy_label.setStyleSheet(pill(pc))

    # ── actions ──
    def run_action(self, action: str):
        if action == "disable":
            answer = QMessageBox.question(
                self, "Disable Windows Update",
                "This will stop and disable all Windows Update services, policies "
                "and scheduled tasks.\n\nYour current settings are backed up and can "
                "be restored with “Enable Updates”.\n\nContinue?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.set_busy(True)
        self.worker = Worker(action)
        self.worker.log.connect(self.add_log)
        self.worker.done.connect(self.on_done)
        self.worker.start()

    def on_done(self, ok: bool):
        self.set_busy(False)
        self.refresh()
        self.worker = None

    def restart_admin(self):
        if relaunch_as_admin():
            QApplication.quit()
        else:
            self.add_log("err", "Elevation was cancelled.")

    def reboot(self):
        answer = QMessageBox.question(self, "Restart PC",
                                      "Restart the computer now?\nSave your work first.")
        if answer == QMessageBox.StandardButton.Yes:
            subprocess.run(["shutdown", "/r", "/t", "5"], creationflags=0x08000000)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Busy", "Please wait for the current operation to finish.")
            event.ignore()
            return
        event.accept()


def main():
    # Ask for elevation once on launch; fall back to read-only if declined.
    if not Disable_Update.if_admin() and "--no-elevate" not in sys.argv:
        sys.argv.append("--no-elevate")  # prevent loops if UAC is declined
        if relaunch_as_admin():
            return

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setFont(QFont("Segoe UI", 10))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
