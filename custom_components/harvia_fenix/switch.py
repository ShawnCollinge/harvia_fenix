from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from homeassistant.components.switch import SwitchEntity
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
    """Coerce common bool-ish values."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(int(val))
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("1", "true", "on", "running", "active", "heating", "started", "start"):
            return True
        if s in ("0", "false", "off", "inactive", "stopped", "stop", "standby", "idle", "ready"):
            return False
    return None


@dataclass(frozen=True)
class HarviaSwitchSpec:
    command: str
    name: str
    icon: str
    # Telemetry key in latest-data["data"] that reflects live on/off state.
    # When None, the switch is the sauna power switch and reads "sauna_status"
    # from the device coordinator instead.
    data_key: Optional[str] = None
    # Capability flag in the normalized device state. When set and the device
    # does not report the function, the entity is created but disabled.
    capability_key: Optional[str] = None


# Command types verified against the Harvia cloud API (POST /devices/command):
#   SAUNA, LIGHTS (note the plural!), FAN, STEAMER are accepted; LIGHT is not.
# Light and fan are exposed as dedicated light/fan platforms; the steamer has no
# matching HA domain, so it stays a switch.
SWITCH_SPECS: list[HarviaSwitchSpec] = [
    HarviaSwitchSpec(command="SAUNA", name="Sauna", icon="mdi:sauna"),
    HarviaSwitchSpec(
        command="STEAMER", name="Steamer", icon="mdi:pot-steam",
        data_key="steamOn", capability_key="has_steamer",
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device_coordinator: HarviaDeviceCoordinator = hass.data[DOMAIN][entry.entry_id][DEVICE_COORDINATOR]
    states = (device_coordinator.data or {}).get("states", {})
    devices: list[HarviaDevice] = (device_coordinator.data or {}).get("devices", [])

    entities: list[SwitchEntity] = []
    for dev in devices:
        state = states.get(dev.id) or {}
        for spec in SWITCH_SPECS:
            # Fail open: unknown capability defaults to enabled.
            supported = bool(state.get(spec.capability_key, True)) if spec.capability_key else True
            entities.append(HarviaSaunaSwitch(hass, entry.entry_id, device_coordinator, dev, spec, supported))

    async_add_entities(entities)


class HarviaSaunaSwitch(CoordinatorEntity[HarviaDeviceCoordinator], SwitchEntity):
    """Controllable Harvia function (sauna power, light, fan, steamer).

    Sauna power follows states[device_id]['sauna_status'] from the device
    coordinator. Light/fan/steamer follow live telemetry (lightOn/fanOn/steamOn)
    from the data coordinator, so those switches also subscribe to it.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        coordinator: HarviaDeviceCoordinator,
        device: HarviaDevice,
        spec: HarviaSwitchSpec,
        supported: bool = True,
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._entry_id = entry_id
        self._device = device
        self._spec = spec

        self._attr_icon = spec.icon
        self._attr_unique_id = f"{device.id}_switch_{spec.command.lower()}"
        self._attr_name = f"Harvia {device.type} {spec.name}"
        self._attr_device_info = build_device_info(device)
        # Devices without this function still get the entity, but disabled.
        self._attr_entity_registry_enabled_default = supported

    @property
    def _data_coordinator(self) -> HarviaDataCoordinator:
        return self._hass.data[DOMAIN][self._entry_id][DATA_COORDINATOR]

    async def async_added_to_hass(self) -> None:
        """Subscribe to the data coordinator for telemetry-backed switches."""
        await super().async_added_to_hass()
        if self._spec.data_key is not None:
            self.async_on_remove(
                self._data_coordinator.async_add_listener(self.async_write_ha_state)
            )

    @property
    def is_on(self) -> Optional[bool]:
        # Light / fan / steamer: live telemetry from the data coordinator.
        if self._spec.data_key is not None:
            data = _get_latest_data_dict(self._data_coordinator, self._device.id)
            if not isinstance(data, dict):
                return None
            return _coerce_bool(data.get(self._spec.data_key))

        # Sauna power: status from the device coordinator.
        state = (self.coordinator.data or {}).get("states", {}).get(self._device.id)
        if not isinstance(state, dict):
            return None

        # Explicit Harvia logic: 1 = ON; 0/2/3 = OFF.
        try:
            iv = int(state.get("sauna_status"))
        except (TypeError, ValueError):
            return None

        if iv == 1:
            return True
        if iv in (0, 2, 3):
            return False
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        payload = {"state": on, "cabin_id": "C1"}

        try:
            resp = await self.coordinator.api.async_send_device_command(
                device_id=self._device.id,
                command=self._spec.command,
                payload=payload,
            )
        except Exception:
            _LOGGER.exception(
                "Harvia SWITCH RESP ERROR device=%s command=%s",
                self._device.id,
                self._spec.command,
            )
            raise

        # Backend/Cloud + Polling: mehrere Refreshes helfen, dass der UI-Status schneller nachzieht
        await self.coordinator.async_request_refresh()
        await self._data_coordinator.async_request_refresh()
        await asyncio.sleep(3)
        await self.coordinator.async_request_refresh()
        await self._data_coordinator.async_request_refresh()
        await asyncio.sleep(6)
        await self.coordinator.async_request_refresh()
        await self._data_coordinator.async_request_refresh()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        payload = _get_latest_payload(self._data_coordinator, self._device.id)
        if not isinstance(payload, dict):
            return None
        return {
            "timestamp": payload.get("timestamp"),
            "shadowName": payload.get("shadowName"),
            "subId": payload.get("subId"),
            "type": payload.get("type"),
        }
