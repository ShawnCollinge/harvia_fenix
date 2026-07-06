from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import EntityCategory

from .constants import DOMAIN, DEVICE_COORDINATOR, DATA_COORDINATOR
from .coordinator import HarviaDeviceCoordinator, HarviaDataCoordinator
from .api import HarviaDevice


from .device_info import build_device_info
from ._helpers import latest_data, coerce_bool, data_attributes


@dataclass(frozen=True)
class HarviaDataBinarySpec:
    data_key: str
    name: str  # include "data_" prefix
    entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC
    disabled_by_default: bool = False


# Binary sensors for response keys that are 0/1 or state-ish.
DATA_BINARY_SPECS: list[HarviaDataBinarySpec] = [
    HarviaDataBinarySpec("fanOn", "data_fanOn"),
    HarviaDataBinarySpec("steamOn", "data_steamOn"),
    HarviaDataBinarySpec("heatOn", "data_heatOn"),
    HarviaDataBinarySpec("lightOn", "data_lightOn"),

    HarviaDataBinarySpec("safetyRelay", "data_safetyRelay", EntityCategory.DIAGNOSTIC),
    HarviaDataBinarySpec("doorSafetyState", "data_doorSafetyState", EntityCategory.DIAGNOSTIC),

    HarviaDataBinarySpec("onOffTrigger", "data_onOffTrigger", EntityCategory.DIAGNOSTIC),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device_coordinator: HarviaDeviceCoordinator = hass.data[DOMAIN][entry.entry_id][DEVICE_COORDINATOR]
    data_coordinator: HarviaDataCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]

    devices: list[HarviaDevice] = (device_coordinator.data or {}).get("devices", [])

    entities: list[BinarySensorEntity] = []
    for dev in devices:
        for spec in DATA_BINARY_SPECS:
            entities.append(HarviaLatestDataBinarySensor(data_coordinator, dev, spec))

    async_add_entities(entities)


class HarviaLatestDataBinarySensor(CoordinatorEntity[HarviaDataCoordinator], BinarySensorEntity):
    """Binary telemetry from latest-data['data'] (static list DATA_BINARY_SPECS)."""

    def __init__(self, coordinator: HarviaDataCoordinator, device: HarviaDevice, spec: HarviaDataBinarySpec) -> None:
        super().__init__(coordinator)
        self._device = device
        self._spec = spec

        self._attr_unique_id = f"{device.id}_{spec.name}"
        self._attr_name = f"Harvia {device.type} {spec.name}"

        if spec.disabled_by_default:
            self._attr_entity_registry_enabled_default = False

        if spec.entity_category is not None:
            self._attr_entity_category = spec.entity_category

        self._attr_device_info = build_device_info(device)

    @property
    def is_on(self) -> Optional[bool]:
        data_dict = latest_data(self.coordinator, self._device.id)
        if not isinstance(data_dict, dict):
            return None
        return coerce_bool(data_dict.get(self._spec.data_key))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return data_attributes(self.coordinator, self._device.id)
