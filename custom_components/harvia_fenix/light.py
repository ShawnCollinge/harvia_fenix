from __future__ import annotations

from typing import Any

from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.core import HomeAssistant

from .api import HarviaDevice
from .entity import HarviaOnOffEntity, make_onoff_setup


class HarviaLight(HarviaOnOffEntity, LightEntity):
    """Sauna cabin light (on/off). State from the device coordinator's light_on."""

    _command = "LIGHTS"
    _state_key = "light_on"
    _attr_icon = "mdi:lightbulb"
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, hass: HomeAssistant, entry_id: str, device: HarviaDevice) -> None:
        super().__init__(hass, entry_id, device)
        self._attr_unique_id = f"{device.id}_light"
        self._attr_name = f"Harvia {device.type} Light"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)


async_setup_entry = make_onoff_setup(HarviaLight)
