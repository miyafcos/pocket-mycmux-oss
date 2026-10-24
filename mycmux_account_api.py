"""Public v0.88 account operations. No credential-file fallback on writes."""
from typing import Any, Callable
import re
import uuid

GROUPS = ("active", "available", "near_limit", "limited", "unverified")
PROFILE = re.compile(r"^(?:claude|codex|grok|unregistered-claude|unregistered-codex|unregistered-grok)-[A-Za-z0-9_.+-]+$")
PRODUCTS = {"cc": "claude", "cx": "codex", "grok": "grok"}


class AccountAPIError(ValueError):
    def __init__(self, code: str, status: int = 503):
        super().__init__(code)
        self.code, self.status = code, status


class AccountAPI:
    def __init__(self, request: Callable[..., Any]):
        self.request = request

    def version(self) -> dict:
        try:
            value = self.request("system.version", {})
        except Exception:
            raise AccountAPIError("accountUnavailable") from None
        if not isinstance(value, dict):
            raise AccountAPIError("accountUnavailable")
        return value

    def usage(self) -> dict:
        from mycmux_cli_accounts import accounts_from_usage
        info = self.version()
        capabilities = info.get("capabilities") or []
        if "account.usage.presentation.v1" not in capabilities:
            raise AccountAPIError("accountUnsupported", 409)
        try:
            report = self.request("account.usage", {})
        except Exception:
            raise AccountAPIError("accountUnavailable") from None
        if not isinstance(report, dict) or not isinstance(report.get("accounts"), list):
            raise AccountAPIError("accountUnavailable")
        return {"accounts": accounts_from_usage(report),
                "availability_groups": report.get("availability_groups") or [],
                "capabilities": [cap for cap in capabilities if cap in {
                    "account.switch.v1", "account.use_reset_ticket", "account.usage.presentation.v1"}],
                "api": info.get("api"), "fetched_at": report.get("fetched_at")}

    def mutate(self, payload: Any, *, reset: bool = False) -> dict:
        if not isinstance(payload, dict):
            raise AccountAPIError("accountInvalid", 400)
        provider = payload.get("provider") or PRODUCTS.get(payload.get("product"))
        profile = payload.get("profile_id")
        if provider not in {"claude", "codex", "grok"} or not isinstance(profile, str):
            raise AccountAPIError("accountInvalid", 400)
        live_target = reset and profile == "live:" + provider
        if len(profile) > 160 or (not live_target and not PROFILE.fullmatch(profile)):
            raise AccountAPIError("accountInvalid", 400)
        expected_prefix = (provider + "-", "unregistered-" + provider + "-")
        if not live_target and not profile.startswith(expected_prefix):
            raise AccountAPIError("accountInvalid", 400)
        if reset and payload.get("confirmed") is not True:
            raise AccountAPIError("resetConfirmationRequired", 400)
        args = {"provider": provider, "profile_id": profile}
        command = "account.use_reset_ticket" if reset else "account.switch"
        capability = "account.use_reset_ticket" if reset else "account.switch.v1"
        info = self.version()
        if capability not in (info.get("capabilities") or []):
            raise AccountAPIError("accountUnsupported", 409)
        if reset:
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
                raise AccountAPIError("accountInvalid", 400)
            email = payload.get("expected_email")
            if not isinstance(email, str) or not email.strip():
                raise AccountAPIError("accountInvalid", 400)
            args.update(request_id=request_id, expected_email=email)
        try:
            result = self.request(command, args)
        except Exception:
            # A lost reply is not proof that an irreversible write failed.
            # Never retry through the CLI or restore_snapshot.
            raise AccountAPIError("accountResultUnconfirmed", 502) from None
        statuses = {"used", "unconfirmed", "no_ticket", "not_usable", "rejected", "failed"} if reset else {
            "switched", "already_active", "rejected", "failed"}
        if not isinstance(result, dict) or result.get("status") not in statuses:
            raise AccountAPIError("accountResultUnconfirmed", 502)
        allowed = {"provider", "profile_id", "status", "applied", "active_profile_id", "active_known",
                   "intended_active_profile_id", "wrote_back_to", "warnings", "error_code",
                   "outcome", "resets_left", "expires_at", "tickets_known", "weekly_resets_at",
                   "retry_of_unconfirmed"}
        return {key: value for key, value in result.items() if key in allowed}
