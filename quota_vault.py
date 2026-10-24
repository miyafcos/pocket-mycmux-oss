"""Local snapshots of mycmux-linked logins. Never logs tokens."""
from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_VAULT = ROOT / "_state" / "quota-accounts.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {"cc": [], "cx": [], "grok": []}
    if not isinstance(data, dict):
        return {"cc": [], "cx": [], "grok": []}
    out = {"cc": [], "cx": [], "grok": []}
    for key in out:
        rows = data.get(key)
        if isinstance(rows, list):
            out[key] = [row for row in rows if isinstance(row, dict) and row.get("identity")]
    return out


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(tmp), flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _public_row(row: dict[str, Any], *, current: bool) -> dict[str, Any]:
    return {
        "identity": row.get("identity"),
        "org": row.get("org"),
        "plan": row.get("plan"),
        "live": row.get("live"),
        "grok_key": row.get("grok_key"),
        "current": current,
    }


def load_vault(path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    return _load(path or DEFAULT_VAULT)


def save_vault(data: dict[str, list[dict[str, Any]]], path: Path | None = None) -> None:
    _atomic_write(path or DEFAULT_VAULT, data)


def upsert(product: str, row: dict[str, Any], path: Path | None = None) -> None:
    identity = str(row.get("identity") or "").strip()
    if product not in {"cc", "cx", "grok"} or not identity:
        return
    vault = load_vault(path)
    kept = []
    replaced = False
    for existing in vault[product]:
        if str(existing.get("identity") or "").strip().lower() == identity.lower():
            existing.update(row)
            existing["identity"] = identity
            existing["updated_at"] = _now()
            kept.append(existing)
            replaced = True
        else:
            kept.append(existing)
    if not replaced:
        row = dict(row)
        row["identity"] = identity
        row["updated_at"] = _now()
        kept.append(row)
    vault[product] = kept
    save_vault(vault, path)


def snapshot_live(home: Path, accounts: list[dict[str, Any]], path: Path | None = None) -> None:
    """Store the live login blobs keyed by identity. Does not delete others."""
    for account in accounts:
        if not account.get("ok") or not account.get("current"):
            continue
        product = str(account.get("id") or "")
        identity = str(account.get("identity") or "").strip()
        if not identity:
            continue
        live = _live_blob(home, product, identity)
        if live is None:
            continue
        upsert(product, {
            "identity": identity,
            "org": account.get("org"),
            "plan": account.get("plan"),
            "live": live,
            "grok_key": live.get("grok_key") if product == "grok" else None,
        }, path)


def _live_blob(home: Path, product: str, identity: str) -> dict[str, Any] | None:
    if product == "cc":
        creds = _read(home / ".claude" / ".credentials.json")
        oauth = (_read(home / ".claude.json") or {}).get("oauthAccount")
        if not isinstance(creds, dict) or not creds.get("claudeAiOauth"):
            return None
        blob: dict[str, Any] = {"credentials": creds}
        if isinstance(oauth, dict):
            blob["oauthAccount"] = oauth
        return blob
    if product == "cx":
        auth = _read(home / ".codex" / "auth.json")
        return auth if isinstance(auth, dict) and isinstance(auth.get("tokens"), dict) else None
    if product == "grok":
        auth = _read(home / ".grok" / "auth.json")
        if not isinstance(auth, dict):
            return None
        needle = identity.lower()
        for key, entry in auth.items():
            if isinstance(entry, dict) and str(entry.get("email") or "").strip().lower() == needle:
                return {"grok_key": key, "entry": entry}
        return None
    return None


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(path, payload)


def apply_switch(product: str, identity: str, home: Path, path: Path | None = None) -> None:
    identity = str(identity or "").strip()
    product = str(product or "").strip()
    if product not in {"cc", "cx", "grok"} or not identity:
        raise ValueError("quotaSwitchFailed")
    vault = load_vault(path)
    row = next(
        (
            item for item in vault.get(product, [])
            if str(item.get("identity") or "").strip().lower() == identity.lower()
        ),
        None,
    )
    if row is None or not isinstance(row.get("live"), dict):
        raise ValueError("quotaSwitchFailed")
    live = row["live"]
    if product == "cc":
        creds = live.get("credentials")
        if not isinstance(creds, dict):
            raise ValueError("quotaSwitchFailed")
        _write_json(home / ".claude" / ".credentials.json", creds)
        oauth = live.get("oauthAccount")
        if isinstance(oauth, dict):
            claude_json = _read(home / ".claude.json") or {}
            claude_json["oauthAccount"] = oauth
            _write_json(home / ".claude.json", claude_json)
        return
    if product == "cx":
        if not isinstance(live.get("tokens"), dict):
            raise ValueError("quotaSwitchFailed")
        _write_json(home / ".codex" / "auth.json", live)
        return
    if product == "grok":
        entry = live.get("entry") if isinstance(live.get("entry"), dict) else live
        key = str(live.get("grok_key") or identity)
        if not isinstance(entry, dict) or not entry.get("key"):
            raise ValueError("quotaSwitchFailed")
        _write_json(home / ".grok" / "auth.json", {key: entry})
        return
    raise ValueError("quotaSwitchFailed")


def other_rows(product: str, current_identity: str | None, path: Path | None = None) -> list[dict[str, Any]]:
    current = str(current_identity or "").strip().lower()
    rows = []
    for row in load_vault(path).get(product, []):
        identity = str(row.get("identity") or "").strip()
        if not identity or identity.lower() == current:
            continue
        rows.append(_public_row(row, current=False))
    return rows
