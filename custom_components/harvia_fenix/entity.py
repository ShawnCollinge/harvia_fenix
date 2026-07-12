from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .constants import DOMAIN, DEVICE_COORDINATOR
from .coordinator import HarviaDeviceCoordinator
from .api import HarviaDevice
from .device_info import build_device_info
from ._helpers import coerce_bool

_LOGGER = logging.getLogger(__name__)

_COMMAND_FALLBACK_DELAY = 2.5  # give the websocket push this long before polling (s)


class HarviaOnOffEntity(CoordinatorEntity[HarviaDeviceCoordinator]):
    """Base for Harvia on/off entities.

    State comes from the device coordinator, which is fed in real time by the
    websocket feed (with the slow poll as a fallback), so there's no optimistic
    guessing. Commands go through the devicesCommandsSend GraphQL mutation.
    Subclasses set _command (SAUNA/LIGHTS/FAN) and _state_key (the normalized
    on/off key), or override is_on.
    """

    _command: str = ""
    _state_key: str = ""
    _fallback_unsub: CALLBACK_TYPE | None = None

    def __init__(self, hass: HomeAssistant, entry_id: str, device: HarviaDevice) -> None:
        super().__init__(hass.data[DOMAIN][entry_id][DEVICE_COORDINATOR])
        self._entry_id = entry_id
        self._device = device
        self._attr_device_info = build_device_info(device)

    def _state(self) -> dict[str, Any]:
        state = (self.coordinator.data or {}).get("states", {}).get(self._device.id)
        return state if isinstance(state, dict) else {}

    @property
    def is_on(self) -> Optional[bool]:
        return coerce_bool(self._state().get(self._state_key))

    async def _async_set(self, on: bool) -> None:
        try:
            await self.coordinator.api.async_send_command(self._device.id, self._command, on)
        except Exception:
            _LOGGER.exception(
                "Harvia %s command error device=%s", self._command, self._device.id
            )
            raise
        # State normally arrives via the websocket push within a second or two.
        # Follow up with an unthrottled poll (a plain async_request_refresh
        # would hit the interval throttle and re-serve cached state) so a dead
        # push feed costs seconds, not a full poll interval. One pending timer
        # per entity: rapid commands coalesce, and removal cancels it.
        if self._fallback_unsub:
            self._fallback_unsub()
        self._fallback_unsub = async_call_later(
            self.hass, _COMMAND_FALLBACK_DELAY, self._poll_after_command
        )

    async def _poll_after_command(self, _now) -> None:
        self._fallback_unsub = None
        await self.coordinator.async_force_refresh()

    async def async_will_remove_from_hass(self) -> None:
        if self._fallback_unsub:
            self._fallback_unsub()
            self._fallback_unsub = None
        await super().async_will_remove_from_hass()


def make_onoff_setup(entity_cls: type[HarviaOnOffEntity]):
    """Build an async_setup_entry that adds one on/off entity per device."""

    async def async_setup_entry(
        hass: HomeAssistant,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        device_coordinator = hass.data[DOMAIN][entry.entry_id][DEVICE_COORDINATOR]
        devices = (device_coordinator.data or {}).get("devices", [])
        async_add_entities(entity_cls(hass, entry.entry_id, dev) for dev in devices)

    return async_setup_entry
