from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant

from .api import HarviaDevice
from .entity import HarviaOnOffEntity, make_onoff_setup


class HarviaFan(HarviaOnOffEntity, FanEntity):
    """Sauna ventilation fan (on/off). State follows latest-data['fanOn']."""

    _command = "FAN"
    _state_key = "fan_on"
    _attr_icon = "mdi:fan"
    _attr_supported_features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF

    def __init__(self, hass: HomeAssistant, entry_id: str, device: HarviaDevice) -> None:
        super().__init__(hass, entry_id, device)
        self._attr_unique_id = f"{device.id}_fan"
        self._attr_name = f"Harvia {device.type} Fan"

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)


async_setup_entry = make_onoff_setup(HarviaFan)
