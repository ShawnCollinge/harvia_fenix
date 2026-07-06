"""Shared helpers for the Harvia platforms: reading the latest-data telemetry
payload and coercing on/off-ish values.
"""
from __future__ import annotations

from typing import Any, Optional


def latest_payload(coordinator, device_id: str) -> dict[str, Any] | None:
    """Return the latest-data payload dict for a device, or None."""
    latest_map = coordinator.data.get("latest_data", {}) if coordinator.data else {}
    payload = latest_map.get(device_id)
    return payload if isinstance(payload, dict) else None


def latest_data(coordinator, device_id: str) -> dict[str, Any] | None:
    """Return the inner ['data'] dict of the latest-data payload, or None."""
    payload = latest_payload(coordinator, device_id)
    if not isinstance(payload, dict):
        return None
    d = payload.get("data")
    return d if isinstance(d, dict) else None


def data_attributes(coordinator, device_id: str) -> dict[str, Any] | None:
    """Common extra_state_attributes from the latest-data payload."""
    payload = latest_payload(coordinator, device_id)
    if not isinstance(payload, dict):
        return None
    return {
        "timestamp": payload.get("timestamp"),
        "shadowName": payload.get("shadowName"),
        "subId": payload.get("subId"),
        "type": payload.get("type"),
    }


# Superset of the on/off-ish strings the platforms need. Numeric/bool values are
# handled directly; strings cover both plain on/off and sauna-status wording.
_TRUE_STRINGS = {"1", "true", "on", "running", "active", "heating", "started", "start"}
_FALSE_STRINGS = {"0", "false", "off", "inactive", "stopped", "stop", "standby", "idle", "ready"}


def coerce_bool(val: Any) -> Optional[bool]:
    """Coerce common bool-ish values (bool / int / float / string) to a bool."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(int(val))
    if isinstance(val, str):
        s = val.strip().lower()
        if s in _TRUE_STRINGS:
            return True
        if s in _FALSE_STRINGS:
            return False
    return None
