"""Switch mycmux-registered CLI accounts by restoring local snapshots.

No iPhone login. Tokens never logged.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quota_vault import _atomic_write

MYCMUX_DATA = Path.home() / "AppData" / "Roaming" / "com.miyazaki.mycmux"
SNAPSHOT_DIR = MYCMUX_DATA / "cli_account_snapshots"
REGISTRY = MYCMUX_DATA / "cli_accounts.json"
CLI = Path(__file__).resolve().parent / "scripts" / "mycmux_agent_cli_v088.py"

PROVIDER_TO_PRODUCT = {"claude": "cc", "codex": "cx", "grok": "grok"}
PRODUCT_TO_PROVIDER = {value: key for key, value in PROVIDER_TO_PRODUCT.items()}


def _usage_cli() -> Path:
    # The same configured override as other bridge CLI commands.
    live_server = sys.modules.get("server")
    return getattr(live_server, "BRIDGE_CLI", CLI)


def fetch_registered_usage() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(_usage_cli()), "usage"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    if completed.returncode != 0:
        raise RuntimeError("usage failed")
    data = json.loads(completed.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise RuntimeError("usage shape")
    return data


def _window_from_mycmux(raw: Any, kind: str, *, fraction: bool = True) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("pct") is None:
        return None
    used = raw.get("pct")
    try:
        used_f = float(used)
    except (TypeError, ValueError):
        return None
    if fraction and used_f <= 1:
        used_f *= 100
    reset = raw.get("resets_at")
    return {
        "kind": kind,
        "used_percent": round(max(0.0, min(100.0, used_f)), 1),
        "remaining_percent": round(max(0.0, 100.0 - max(0.0, min(100.0, used_f))), 1),
        "reset_at": reset if isinstance(reset, str) else None,
    }


def accounts_from_usage(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in data.get("accounts") or []:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "")
        product = PROVIDER_TO_PRODUCT.get(provider)
        if not product:
            continue
        windows = []
        for key, kind in (("five_hour", "five_hour"), ("seven_day", "weekly"), ("seven_day_opus", "weekly_opus")):
            window = _window_from_mycmux(item.get(key), kind, fraction=item.get("availability_group") is None)
            if window:
                windows.append(window)
        error = None
        if item.get("needs_relogin"):
            error = "auth"
        elif item.get("error_code") and not windows:
            error = "unavailable"
        rows.append({
            "id": product,
            "product": product,
            "ok": bool(windows) and error != "auth",
            "current": bool(item.get("is_active")),
            "identity": str(item.get("email") or item.get("label") or "").strip() or None,
            "org": str(item.get("label") or "").strip() or None,
            "plan": str(item.get("plan") or "").strip().lower() or None,
            "profile_id": str(item.get("profile_id") or ""),
            "needs_relogin": bool(item.get("needs_relogin")),
            "windows": windows,
            "provider": provider,
            "email": item.get("email") if isinstance(item.get("email"), str) else None,
            "registered": item.get("registered") is True,
            "state": item.get("state"),
            "availability_group": item.get("availability_group"),
            "availability_order": item.get("availability_order"),
            "availability_label": item.get("availability_label"),
            "reset_tickets": item.get("reset_tickets") if isinstance(item.get("reset_tickets"), dict) else None,
            **({"error": error} if error else {}),
        })
    return rows


def restore_snapshot(
    profile_id: str,
    home: Path | None = None,
    *,
    snapshot_dir: Path | None = None,
    registry: Path | None = None,
) -> None:
    profile_id = str(profile_id or "").strip()
    if not profile_id or "/" in profile_id or "\\" in profile_id or ".." in profile_id:
        raise ValueError("quotaSwitchFailed")
    path = (snapshot_dir or SNAPSHOT_DIR) / f"{profile_id}.json"
    if not path.is_file():
        raise ValueError("quotaSwitchFailed")
    stored = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(stored, dict):
        raise ValueError("quotaSwitchFailed")
    root = home or Path.home()
    if "credentials_text" in stored:
        creds = stored["credentials_text"]
        json.loads(creds)
        _atomic_write(root / ".claude" / ".credentials.json", json.loads(creds))
        oauth_text = stored.get("oauth_account_text") or ""
        if str(oauth_text).strip():
            claude_json_path = root / ".claude.json"
            doc = json.loads(claude_json_path.read_text(encoding="utf-8")) if claude_json_path.exists() else {}
            doc["oauthAccount"] = json.loads(oauth_text)
            _atomic_write(claude_json_path, doc)
    elif "auth_text" in stored:
        auth = json.loads(stored["auth_text"])
        if not isinstance(auth.get("tokens"), dict):
            raise ValueError("quotaSwitchFailed")
        _atomic_write(root / ".codex" / "auth.json", auth)
    elif "grok_auth_text" in stored:
        grok = json.loads(stored["grok_auth_text"])
        if not isinstance(grok, dict) or not grok:
            raise ValueError("quotaSwitchFailed")
        _atomic_write(root / ".grok" / "auth.json", grok)
    else:
        raise ValueError("quotaSwitchFailed")
    _mark_active(profile_id, registry or REGISTRY)


def _mark_active(profile_id: str, registry: Path = REGISTRY) -> None:
    if not registry.is_file():
        return
    data = json.loads(registry.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return
    profiles = data.get("profiles") if isinstance(data.get("profiles"), list) else []
    provider = None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in profiles:
        if isinstance(row, dict) and row.get("id") == profile_id:
            provider = row.get("provider")
            row["last_switched_at"] = now
            break
    if provider in {"claude", "codex", "grok"}:
        active = data.get("active") if isinstance(data.get("active"), dict) else {}
        active[provider] = profile_id
        data["active"] = active
    _atomic_write(registry, data)
