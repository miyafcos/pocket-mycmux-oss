"""ADR 0017: placement follows the wire API version, never the app version."""
from typing import Any


def api_version(payload: Any) -> dict[str, int]:
    raw = payload.get("api") if isinstance(payload, dict) else None
    if isinstance(raw, dict):
        major, minor = raw.get("major"), raw.get("minor")
        if (isinstance(major, int) and not isinstance(major, bool) and major >= 1
                and isinstance(minor, int) and not isinstance(minor, bool) and minor >= 0):
            return {"major": major, "minor": minor}
    return {"major": 1, "minor": 0}


def container_layout(payload: Any) -> bool:
    version = api_version(payload)
    return (version["major"], version["minor"]) >= (1, 1)


def placement_columns(payload: dict[str, Any]) -> list[list[str]]:
    columns = payload.get("splitColumns") or []
    if not container_layout(payload):
        return columns
    # 1.1 redefines splitColumns as container order. 2.0 removes vertical rows.
    # ADR 0017 leaves exact new visible-column field names unspecified.
    # Pocket lists containers, so it needs no guessed visible-column fields.
    ids = [item for column in columns for item in column]
    return [[item] for item in dict.fromkeys(ids)]
