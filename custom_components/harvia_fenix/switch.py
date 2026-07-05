from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant

from .api import HarviaDevice
from .entity import HarviaOnOffEntity, make_onoff_setup


class HarviaSaunaSwitch(HarviaOnOffEntity, SwitchEntity):
    """Sauna power switch. Status follows states[device_id]['sauna_status'] (device coordinator)."""

    _command = "SAUNA"
    _attr_icon = "mdi:sauna"

    def __init__(self, hass: HomeAssistant, entry_id: str, device: HarviaDevice) -> None:
        super().__init__(hass, entry_id, device)
        self._attr_unique_id = f"{device.id}_switch_sauna"
        self._attr_name = f"Harvia {device.type} Sauna"

    def _read_state(self) -> Optional[bool]:
        state = (self.coordinator.data or {}).get("states", {}).get(self._device.id)
        if not isinstance(state, dict):
            return None
        # Harvia sauna_status: 1 = ON; 0/2/3 = OFF.
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


async_setup_entry = make_onoff_setup(HarviaSaunaSwitch)
