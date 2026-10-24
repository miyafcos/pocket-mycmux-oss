"""Device-code login for adding a mycmux-linked account from pocket."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

URL_RE = re.compile(r"https://[^\s>\"']+")
CODE_RE = re.compile(
    r"(?:user_code=|enter code:?\s*|code:\s*)([A-Z0-9]{4,}(?:-[A-Z0-9]{4,})*)",
    re.IGNORECASE,
)

COMMANDS = {
    "cc": ["claude", "auth", "login", "--claudeai"],
    "cx": ["codex", "login", "--device-auth"],
    "grok": ["grok", "login", "--device-auth"],
}

CREATE_NO_WINDOW = 0x08000000


def parse_device_output(text: str) -> dict[str, str | None]:
    url = None
    code = None
    for match in URL_RE.finditer(text or ""):
        candidate = match.group(0).rstrip(").,")
        url = candidate
        query = re.search(r"user_code=([A-Z0-9-]+)", candidate, re.IGNORECASE)
        if query:
            code = query.group(1)
    found = CODE_RE.search(text or "")
    if found:
        code = found.group(1)
    return {"url": url, "code": code}


def _command(product: str) -> list[str]:
    parts = list(COMMANDS[product])
    resolved = shutil.which(parts[0])
    if resolved:
        parts[0] = resolved
    return parts


class QuotaLogin:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._product: str | None = None
        self._url: str | None = None
        self._code: str | None = None
        self._phase = "idle"
        self._output = ""

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._finish_if_exited_locked()
            return {
                "phase": self._phase,
                "product": self._product,
                "url": self._url,
                "code": self._code,
            }

    def start(self, product: str) -> dict[str, Any]:
        product = str(product or "").strip()
        if product not in COMMANDS:
            raise ValueError("quotaSwitchFailed")
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise ValueError("quotaLoginBusy")
            self._proc = None
            self._product = product
            self._url = None
            self._code = None
            self._phase = "waiting"
            self._output = ""
            creation = CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.Popen(
                _command(product),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creation,
            )
            self._proc = proc
            thread = threading.Thread(target=self._read, args=(proc,), daemon=True)
            thread.start()
        return self.status()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
            self._phase = "idle"
            self._proc = None
            self._product = None
            return {
                "phase": self._phase,
                "product": None,
                "url": None,
                "code": None,
            }

    def _read(self, proc: subprocess.Popen[str]) -> None:
        stream = proc.stdout
        if stream is None:
            return
        while True:
            line = stream.readline()
            if line == "":
                break
            with self._lock:
                self._output += line
                parsed = parse_device_output(self._output)
                if parsed.get("url"):
                    self._url = parsed["url"]
                if parsed.get("code"):
                    self._code = parsed["code"]
        proc.wait()
        with self._lock:
            if self._proc is proc:
                self._phase = "done" if proc.returncode == 0 else "failed"
                self._proc = None

    def _finish_if_exited_locked(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = proc.poll()
        if code is None:
            return
        self._phase = "done" if code == 0 else "failed"
        self._proc = None


LOGIN = QuotaLogin()
