from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List

from dotenv import load_dotenv


TRUTHY = {"1", "true", "yes", "on"}


def _env(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in TRUTHY


def _has(name: str) -> bool:
    value = _env(name)
    if not value:
        return False
    lowered = value.lower()
    return "your_" not in lowered and lowered not in {"changeme", "example"}


@dataclass
class ConfigValidationReport:
    component: str
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> Dict[str, object]:
        return {
            "component": self.component,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _missing(required_keys: Iterable[str]) -> List[str]:
    return [key for key in required_keys if not _has(key)]


def validate_runtime_env(
    component: str,
    *,
    load_env_file: bool = True,
) -> ConfigValidationReport:
    if load_env_file:
        load_dotenv()
    component_key = str(component or "").strip().lower()
    report = ConfigValidationReport(component=component_key or "unknown")

    auth_enabled = _env_bool("POLYWEATHER_AUTH_ENABLED", False)
    auth_required = _env_bool("POLYWEATHER_AUTH_REQUIRED", auth_enabled)
    auth_require_subscription = _env_bool(
        "POLYWEATHER_AUTH_REQUIRE_SUBSCRIPTION",
        False,
    )
    entitlement_guard = _env_bool("POLYWEATHER_REQUIRE_ENTITLEMENT", False)
    payment_enabled = _env_bool("POLYWEATHER_PAYMENT_ENABLED", False)
    weekly_reward_enabled = _env_bool("POLYWEATHER_WEEKLY_REWARD_ENABLED", False)
    wallet_activity_enabled = _env_bool("POLYMARKET_WALLET_ACTIVITY_ENABLED", False)
    polygon_watch_enabled = _env_bool("POLYGON_WALLET_WATCH_ENABLED", False)

    if component_key == "bot":
        missing = _missing(["TELEGRAM_BOT_TOKEN"])
        if missing:
            report.errors.append(f"Bot startup missing required env vars: {', '.join(missing)}")
        if not (_has("TELEGRAM_CHAT_ID") or _has("TELEGRAM_CHAT_IDS")):
            report.warnings.append("TELEGRAM_CHAT_ID / TELEGRAM_CHAT_IDS not configured — bot push target is empty")

    if auth_enabled:
        missing = _missing(["SUPABASE_URL", "SUPABASE_ANON_KEY"])
        if missing:
            report.errors.append(f"Auth enabled but missing env vars: {', '.join(missing)}")
        if auth_required or auth_require_subscription or weekly_reward_enabled:
            missing = _missing(["SUPABASE_SERVICE_ROLE_KEY"])
            if missing:
                report.errors.append(f"Auth/subscription features require env vars: {', '.join(missing)}")

    if entitlement_guard:
        missing = _missing(["POLYWEATHER_BACKEND_ENTITLEMENT_TOKEN"])
        if missing:
            report.errors.append(f"Backend entitlement guard enabled but missing env vars: {', '.join(missing)}")

    if payment_enabled:
        payment_missing = _missing(["POLYWEATHER_PAYMENT_RPC_URL"])
        if payment_missing:
            report.errors.append(f"Payment enabled but missing env vars: {', '.join(payment_missing)}")
        has_receiver = _has("POLYWEATHER_PAYMENT_RECEIVER_CONTRACT")
        has_tokens_json = _has("POLYWEATHER_PAYMENT_ACCEPTED_TOKENS_JSON")
        if not (has_receiver or has_tokens_json):
            report.errors.append(
                "Payment enabled but POLYWEATHER_PAYMENT_RECEIVER_CONTRACT or POLYWEATHER_PAYMENT_ACCEPTED_TOKENS_JSON not configured"
            )

    if wallet_activity_enabled:
        if not _has("POLYMARKET_WALLET_ACTIVITY_USERS"):
            report.warnings.append("Wallet activity watcher enabled but POLYMARKET_WALLET_ACTIVITY_USERS not configured")

    if polygon_watch_enabled:
        if not _has("POLYGON_WALLET_WATCH_ADDRESSES"):
            report.warnings.append("Polygon watcher enabled but POLYGON_WALLET_WATCH_ADDRESSES not configured")

    if component_key == "web":
        if auth_enabled and not auth_required:
            report.warnings.append("Web auth is in optional mode — login not enforced")

    return report


def validate_or_raise(component: str) -> ConfigValidationReport:
    report = validate_runtime_env(component, load_env_file=True)
    if report.errors:
        raise RuntimeError(" | ".join(report.errors))
    return report
