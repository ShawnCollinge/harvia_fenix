from __future__ import annotations

import asyncio
from typing import Any, Optional

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .constants import DOMAIN, DEVICE_COORDINATOR, DATA_COORDINATOR
from .coordinator import HarviaDeviceCoordinator, HarviaDataCoordinator
from .api import HarviaDevice
from .device_info import build_device_info

import logging
_LOGGER = logging.getLogger(__name__)


def _get_latest_payload(coordinator: HarviaDataCoordinator, device_id: str) -> dict[str, Any] | None:
    latest_map = coordinator.data.get("latest_data", {}) if coordinator.data else {}
    payload = latest_map.get(device_id)
    return payload if isinstance(payload, dict) else None


def _get_latest_data_dict(coordinator: HarviaDataCoordinator, device_id: str) -> dict[str, Any] | None:
    payload = _get_latest_payload(coordinator, device_id)
    if not isinstance(payload, dict):
        return None
    d = payload.get("data")
    return d if isinstance(d, dict) else None


def _coerce_bool(val: Any) -> Optional[bool]:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(int(val))
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("1", "true", "on"):
            return True
        if s in ("0", "false", "off"):
            return False
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device_coordinator: HarviaDeviceCoordinator = hass.data[DOMAIN][entry.entry_id][DEVICE_COORDINATOR]
    data_coordinator: HarviaDataCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]

    states = (device_coordinator.data or {}).get("states", {})
    devices: list[HarviaDevice] = (device_coordinator.data or {}).get("devices", [])

    entities: list[FanEntity] = []
    for dev in devices:
        # Fail open: if the capability flag is missing, still create an enabled fan.
        supported = bool((states.get(dev.id) or {}).get("has_fan", True))
        entities.append(HarviaFan(hass, entry.entry_id, data_coordinator, dev, supported))

    async_add_entities(entities)


class HarviaFan(CoordinatorEntity[HarviaDataCoordinator], FanEntity):
    """Sauna ventilation fan (on/off only). State follows latest-data['fanOn']."""

    _attr_icon = "mdi:fan"
    # On/off only. TURN_ON/TURN_OFF must be declared explicitly (HA 2024.8+),
    # otherwise the fan.turn_on / fan.turn_off actions are rejected.
    _attr_supported_features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        coordinator: HarviaDataCoordinator,
        device: HarviaDevice,
        supported: bool,
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._entry_id = entry_id
        self._device = device

        self._attr_unique_id = f"{device.id}_fan"
        self._attr_name = f"Harvia {device.type} Fan"
        self._attr_device_info = build_device_info(device)
        # Devices without this function still get the entity, but disabled.
        self._attr_entity_registry_enabled_default = supported

    @property
    def _device_coordinator(self) -> HarviaDeviceCoordinator:
        return self._hass.data[DOMAIN][self._entry_id][DEVICE_COORDINATOR]

    @property
    def is_on(self) -> Optional[bool]:
        data = _get_latest_data_dict(self.coordinator, self._device.id)
        if not isinstance(data, dict):
            return None
        return _coerce_bool(data.get("fanOn"))

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        try:
            await self.coordinator.api.async_send_device_command(
                device_id=self._device.id,
                command="FAN",
                payload={"state": on, "cabin_id": "C1"},
            )
        except Exception:
            _LOGGER.exception("Harvia FAN command error device=%s", self._device.id)
            raise

        # Cloud + polling: nudge both coordinators so the UI catches up quickly.
        await self._device_coordinator.async_request_refresh()
        await self.coordinator.async_request_refresh()
        await asyncio.sleep(3)
        await self._device_coordinator.async_request_refresh()
        await self.coordinator.async_request_refresh()
        await asyncio.sleep(6)
        await self._device_coordinator.async_request_refresh()
        await self.coordinator.async_request_refresh()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        payload = _get_latest_payload(self.coordinator, self._device.id)
        if not isinstance(payload, dict):
            return None
        return {
            "timestamp": payload.get("timestamp"),
            "shadowName": payload.get("shadowName"),
            "subId": payload.get("subId"),
            "type": payload.get("type"),
        }
