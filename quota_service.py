"""Subscription windows for the mycmux-linked Claude, Codex, and Grok logins.

Reads the same OAuth files those CLIs use. Never logs tokens.
Does not refresh tokens (Claude Code / Codex / Grok own rotation).
"""
from __future__ import annotations

import base64
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
SSL_CONTEXT = ssl.create_default_context()
CACHE_SECONDS = 45.0
HTTP_TIMEOUT = 18.0

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"

FIVE_HOUR_SECONDS = 8 * 3600
WEEK_SECONDS = 6 * 24 * 3600

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"expires": 0.0, "payload": None}


def _home() -> Path:
    return Path.home()


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    pad = "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except (ValueError, json.JSONDecodeError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean_label(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _iso_jst(value: Any) -> str | None:
    dt = _parse_dt(value)
    if dt is None:
        return None
    return dt.astimezone(JST).isoformat(timespec="minutes")


def _parse_dt(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _percent(value: Any, *, fraction: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    used = float(value)
    if fraction and 0 <= used <= 1:
        used *= 100
    if used < 0:
        used = 0.0
    if used > 100:
        used = 100.0
    return round(used, 1)


def _window(kind: str, used: float | None, reset_at: Any) -> dict[str, Any]:
    remaining = None if used is None else round(max(0.0, 100.0 - used), 1)
    return {
        "kind": kind,
        "used_percent": used,
        "remaining_percent": remaining,
        "reset_at": _iso_jst(reset_at),
    }


def _kind_from_seconds(seconds: Any) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "weekly"
    if float(seconds) <= FIVE_HOUR_SECONDS:
        return "five_hour"
    return "weekly"


def _account(
    product: str,
    *,
    ok: bool,
    plan: str | None = None,
    windows: list[dict[str, Any]] | None = None,
    error: str | None = None,
    identity: str | None = None,
    org: str | None = None,
    current: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": product,
        "product": product,
        "ok": ok,
        "current": current,
        "windows": windows or [],
    }
    if plan:
        payload["plan"] = plan
    if identity:
        payload["identity"] = identity
    if org:
        payload["org"] = org
    if error:
        payload["error"] = error
    return payload


def _http_json(url: str, headers: dict[str, str]) -> tuple[int, dict[str, Any] | None]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT, context=SSL_CONTEXT) as response:
            raw = response.read()
            status = int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 0, None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return status, None
    return status, body if isinstance(body, dict) else None


def claude_token(home: Path | None = None) -> str:
    data = _load_json((home or _home()) / ".claude" / ".credentials.json") or {}
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return ""
    return str(oauth.get("accessToken") or "").strip()


def claude_plan(home: Path | None = None) -> str | None:
    data = _load_json((home or _home()) / ".claude" / ".credentials.json") or {}
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    plan = str(oauth.get("subscriptionType") or "").strip().lower()
    return plan or None


def claude_identity(home: Path | None = None) -> tuple[str | None, str | None]:
    data = _load_json((home or _home()) / ".claude.json") or {}
    account = data.get("oauthAccount")
    if not isinstance(account, dict):
        return None, None
    return _clean_label(account.get("emailAddress")), _clean_label(account.get("organizationName"))


def codex_auth(home: Path | None = None) -> tuple[str, str]:
    data = _load_json((home or _home()) / ".codex" / "auth.json") or {}
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return "", ""
    token = str(tokens.get("access_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip()
    return token, account_id


def codex_identity(home: Path | None = None) -> tuple[str | None, str | None]:
    data = _load_json((home or _home()) / ".codex" / "auth.json") or {}
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    claims = _jwt_claims(str((tokens or {}).get("id_token") or ""))
    email = _clean_label(claims.get("email"))
    name = _clean_label(claims.get("name"))
    if not email:
        profile = claims.get("https://api.openai.com/profile")
        if isinstance(profile, dict):
            email = _clean_label(profile.get("email"))
    return email, name


def grok_entries(home: Path | None = None) -> list[dict[str, Any]]:
    data = _load_json((home or _home()) / ".grok" / "auth.json") or {}
    entries = []
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        entries.append(entry)
    entries.sort(key=lambda item: str(item.get("expires_at") or ""), reverse=True)
    return entries


def fetch_claude(home: Path | None = None) -> dict[str, Any]:
    token = claude_token(home)
    plan = claude_plan(home)
    identity, org = claude_identity(home)
    if not token:
        return _account("cc", ok=False, plan=plan, identity=identity, org=org, error="no_login")
    status, body = _http_json(
        CLAUDE_USAGE_URL,
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-cli/2.1.0",
        },
    )
    if status in {401, 403}:
        return _account("cc", ok=False, plan=plan, identity=identity, org=org, error="auth")
    if status != 200 or body is None:
        return _account("cc", ok=False, plan=plan, identity=identity, org=org, error="unavailable")
    mapping = (
        ("five_hour", "five_hour"),
        ("seven_day", "weekly"),
        ("seven_day_opus", "weekly_opus"),
    )
    windows = []
    for key, kind in mapping:
        raw = body.get(key)
        if not isinstance(raw, dict):
            continue
        used = _percent(raw.get("utilization"), fraction=True)
        if used is None:
            continue
        windows.append(_window(kind, used, raw.get("resets_at")))
    if not windows:
        return _account("cc", ok=False, plan=plan, identity=identity, org=org, error="unavailable")
    return _account("cc", ok=True, plan=plan, identity=identity, org=org, windows=windows)


def fetch_codex(home: Path | None = None) -> dict[str, Any]:
    token, account_id = codex_auth(home)
    identity, org = codex_identity(home)
    if not token:
        return _account("cx", ok=False, identity=identity, org=org, error="no_login")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    status, body = _http_json(CODEX_USAGE_URL, headers)
    if status in {401, 403}:
        return _account("cx", ok=False, identity=identity, org=org, error="auth")
    if status != 200 or body is None:
        return _account("cx", ok=False, identity=identity, org=org, error="unavailable")
    api_email = _clean_label(body.get("email")) if isinstance(body, dict) else None
    identity = identity or api_email
    plan = str(body.get("plan_type") or "").strip().lower() or None
    rate = body.get("rate_limit") if isinstance(body.get("rate_limit"), dict) else {}
    windows = []
    for key in ("primary_window", "secondary_window"):
        raw = rate.get(key)
        if not isinstance(raw, dict):
            continue
        used = _percent(raw.get("used_percent"))
        if used is None:
            continue
        kind = _kind_from_seconds(raw.get("limit_window_seconds"))
        windows.append(_window(kind, used, raw.get("reset_at")))
    if not windows:
        return _account("cx", ok=False, plan=plan, identity=identity, org=org, error="unavailable")
    return _account("cx", ok=True, plan=plan, identity=identity, org=org, windows=windows)


def fetch_grok_entry(entry: dict[str, Any], *, current: bool) -> dict[str, Any]:
    token = str(entry.get("key") or "").strip()
    identity = _clean_label(entry.get("email"))
    if not token:
        return _account("grok", ok=False, identity=identity, current=current, error="no_login")
    status, body = _http_json(
        GROK_BILLING_URL,
        {"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    if status in {401, 403}:
        return _account("grok", ok=False, identity=identity, current=current, error="auth")
    if status != 200 or body is None:
        return _account("grok", ok=False, identity=identity, current=current, error="unavailable")
    cfg = body.get("config") if isinstance(body.get("config"), dict) else {}
    used = _percent(cfg.get("creditUsagePercent"))
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    reset_at = period.get("end") or cfg.get("billingPeriodEnd")
    if used is None:
        return _account("grok", ok=False, identity=identity, current=current, error="unavailable")
    return _account("grok", ok=True, identity=identity, current=current, windows=[_window("weekly", used, reset_at)])


def fetch_grok(home: Path | None = None) -> list[dict[str, Any]]:
    entries = grok_entries(home)
    if not entries:
        return [_account("grok", ok=False, error="no_login")]
    rows = []
    for index, entry in enumerate(entries):
        rows.append(fetch_grok_entry(entry, current=index == 0))
    return rows


def _public_account(account: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in account.items() if key != "live"}


def fetch_stored(product: str, row: dict[str, Any]) -> dict[str, Any]:
    live = row.get("live") if isinstance(row.get("live"), dict) else {}
    identity = _clean_label(row.get("identity"))
    org = _clean_label(row.get("org"))
    plan = _clean_label(row.get("plan"))
    if product == "cc":
        creds = live.get("credentials") if isinstance(live.get("credentials"), dict) else {}
        oauth = creds.get("claudeAiOauth") if isinstance(creds.get("claudeAiOauth"), dict) else {}
        token = str(oauth.get("accessToken") or "").strip()
        if not token:
            return _account("cc", ok=False, identity=identity, org=org, plan=plan, current=False, error="auth")
        return _claude_from_token(token, identity=identity, org=org, plan=plan, current=False)
    if product == "cx":
        tokens = live.get("tokens") if isinstance(live.get("tokens"), dict) else {}
        token = str(tokens.get("access_token") or "").strip()
        account_id = str(tokens.get("account_id") or "").strip()
        if not token:
            return _account("cx", ok=False, identity=identity, org=org, plan=plan, current=False, error="auth")
        return _codex_from_token(token, account_id, identity=identity, org=org, current=False)
    if product == "grok":
        entry = live.get("entry") if isinstance(live.get("entry"), dict) else live
        if not isinstance(entry, dict):
            entry = {}
        return fetch_grok_entry(entry, current=False)
    return _account(product, ok=False, identity=identity, current=False, error="unavailable")


def _claude_from_token(token: str, *, identity: str | None, org: str | None, plan: str | None, current: bool) -> dict[str, Any]:
    status, body = _http_json(
        CLAUDE_USAGE_URL,
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-cli/2.1.0",
        },
    )
    if status in {401, 403}:
        return _account("cc", ok=False, plan=plan, identity=identity, org=org, current=current, error="auth")
    if status != 200 or body is None:
        return _account("cc", ok=False, plan=plan, identity=identity, org=org, current=current, error="unavailable")
    windows = []
    for key, kind in (("five_hour", "five_hour"), ("seven_day", "weekly"), ("seven_day_opus", "weekly_opus")):
        raw = body.get(key)
        if not isinstance(raw, dict):
            continue
        used = _percent(raw.get("utilization"), fraction=True)
        if used is None:
            continue
        windows.append(_window(kind, used, raw.get("resets_at")))
    if not windows:
        return _account("cc", ok=False, plan=plan, identity=identity, org=org, current=current, error="unavailable")
    return _account("cc", ok=True, plan=plan, identity=identity, org=org, current=current, windows=windows)


def _codex_from_token(token: str, account_id: str, *, identity: str | None, org: str | None, current: bool) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    status, body = _http_json(CODEX_USAGE_URL, headers)
    if status in {401, 403}:
        return _account("cx", ok=False, identity=identity, org=org, current=current, error="auth")
    if status != 200 or body is None:
        return _account("cx", ok=False, identity=identity, org=org, current=current, error="unavailable")
    plan = str(body.get("plan_type") or "").strip().lower() or None
    identity = identity or _clean_label(body.get("email"))
    rate = body.get("rate_limit") if isinstance(body.get("rate_limit"), dict) else {}
    windows = []
    for key in ("primary_window", "secondary_window"):
        raw = rate.get(key)
        if not isinstance(raw, dict):
            continue
        used = _percent(raw.get("used_percent"))
        if used is None:
            continue
        windows.append(_window(_kind_from_seconds(raw.get("limit_window_seconds")), used, raw.get("reset_at")))
    if not windows:
        return _account("cx", ok=False, plan=plan, identity=identity, org=org, current=current, error="unavailable")
    return _account("cx", ok=True, plan=plan, identity=identity, org=org, current=current, windows=windows)


def _fetch_all(home: Path | None = None, vault_path: Path | None = None) -> dict[str, Any]:
    import quota_vault

    root = home or _home()
    results: dict[str, Any] = {}

    def run(name: str, worker: Callable[[], Any]) -> None:
        try:
            results[name] = worker()
        except Exception:
            results[name] = [_account(name, ok=False, error="unavailable")]

    workers = (
        ("cc", lambda: [fetch_claude(root)]),
        ("cx", lambda: [fetch_codex(root)]),
        ("grok", lambda: fetch_grok(root)),
    )
    threads = [threading.Thread(target=run, args=item, daemon=True) for item in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(HTTP_TIMEOUT + 2)
    accounts: list[dict[str, Any]] = []
    for name in ("cc", "cx", "grok"):
        rows = results.get(name)
        if isinstance(rows, list):
            accounts.extend(rows)
        elif isinstance(rows, dict):
            accounts.append(rows)
        else:
            accounts.append(_account(name, ok=False, error="unavailable"))
    try:
        quota_vault.snapshot_live(root, accounts, vault_path)
    except Exception:
        pass
    extras: list[dict[str, Any]] = []

    def extra_worker(product: str, row: dict[str, Any]) -> None:
        extras.append(fetch_stored(product, row))

    extra_threads = []
    current_by_product = {}
    for account in accounts:
        if account.get("current") and account.get("identity"):
            current_by_product[account["id"]] = str(account["identity"])
    for product in ("cc", "cx", "grok"):
        for row in quota_vault.other_rows(product, current_by_product.get(product), vault_path):
            thread = threading.Thread(target=extra_worker, args=(product, row), daemon=True)
            extra_threads.append(thread)
            thread.start()
    for thread in extra_threads:
        thread.join(HTTP_TIMEOUT + 2)
    accounts.extend(extras)
    now = datetime.now(JST).isoformat(timespec="minutes")
    return {"fetched_at": now, "accounts": [_public_account(row) for row in accounts]}


def quota_payload(home: Path | None = None, *, force: bool = False, vault_path: Path | None = None) -> dict[str, Any]:
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get("payload")
        if not force and cached is not None and now < float(_CACHE.get("expires") or 0):
            return cached
        payload = None
        if home is None:
            try:
                import mycmux_cli_accounts
                usage = mycmux_cli_accounts.fetch_registered_usage()
                payload = {
                    "fetched_at": datetime.now(JST).isoformat(timespec="minutes"),
                    "accounts": mycmux_cli_accounts.accounts_from_usage(usage),
                }
            except Exception:
                payload = None
        if payload is None:
            payload = _fetch_all(home, vault_path)
        _CACHE["payload"] = payload
        _CACHE["expires"] = now + CACHE_SECONDS
        return payload


def switch_account(
    product: str,
    identity: str,
    home: Path | None = None,
    vault_path: Path | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    # Live operation belongs to mycmux, which saves the outgoing identity.
    # Explicit synthetic homes retain the old seam for offline fixture tests.
    if home is None:
        from mycmux_account_api import AccountAPI
        from mycmux_socket import MycmuxSocket
        result = AccountAPI(MycmuxSocket().request).mutate({
            "product": product, "profile_id": profile_id})
        clear_cache()
        return result
    pid = str(profile_id or "").strip()
    if not pid:
        import mycmux_cli_accounts
        usage = mycmux_cli_accounts.fetch_registered_usage()
        product = str(product or "")
        identity_l = str(identity or "").strip().lower()
        for row in mycmux_cli_accounts.accounts_from_usage(usage):
            if row.get("product") == product and str(row.get("identity") or "").strip().lower() == identity_l:
                pid = str(row.get("profile_id") or "")
                break
    if not pid:
        raise ValueError("quotaSwitchFailed")
    import mycmux_cli_accounts
    mycmux_cli_accounts.restore_snapshot(pid, home or _home())
    clear_cache()
    return quota_payload(home, force=True, vault_path=vault_path)


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE["payload"] = None
        _CACHE["expires"] = 0.0
