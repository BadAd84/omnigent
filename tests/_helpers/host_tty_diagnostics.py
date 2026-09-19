"""Opt-in, read-only PTY diagnostics for host shutdown failures."""

from __future__ import annotations

import json
import os
import signal
import termios
from pathlib import Path

import pexpect
import psutil


def _report(child: pexpect.spawn, event: str) -> None:
    details = {"event": event, "child_pid": child.pid}
    try:
        attrs = termios.tcgetattr(child.child_fd)
        details.update(
            child_pgid=os.getpgid(child.pid),
            foreground_pgid=os.tcgetpgrp(child.child_fd),
            isig=bool(attrs[3] & termios.ISIG),
            icanon=bool(attrs[3] & termios.ICANON),
            echo=bool(attrs[3] & termios.ECHO),
            vintr=repr(attrs[6][termios.VINTR]),
            parent_sigint=str(signal.getsignal(signal.SIGINT)),
        )
        status = Path(f"/proc/{child.pid}/status")
        if status.exists():
            details["signals"] = [
                line for line in status.read_text().splitlines() if line.startswith("Sig")
            ]
        descendants = []
        for process in psutil.Process(child.pid).children(recursive=True):
            try:
                descendants.append(
                    {"pid": process.pid, "name": process.name(), "status": process.status()}
                )
            except psutil.Error:
                continue
        details["descendants"] = descendants
    except (OSError, psutil.Error) as exc:
        details["error"] = str(exc)
    os.write(2, ("PTY_DIAGNOSTIC " + json.dumps(details) + "\n").encode())


def pytest_sessionstart(session) -> None:
    original_send = pexpect.spawn.send
    original_sendcontrol = pexpect.spawn.sendcontrol
    original_close = pexpect.spawn.close

    def send(child, value):
        if value in ("y\r", "n\r"):
            _report(child, "before_send_" + repr(value))
        return original_send(child, value)

    def sendcontrol(child, char):
        _report(child, "before_sendcontrol_" + char)
        return original_sendcontrol(child, char)

    def close(child, *args, **kwargs):
        if not child.closed:
            _report(child, "before_close")
        return original_close(child, *args, **kwargs)

    pexpect.spawn.send = send
    pexpect.spawn.sendcontrol = sendcontrol
    pexpect.spawn.close = close
