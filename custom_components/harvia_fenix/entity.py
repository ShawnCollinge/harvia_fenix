from __future__ import annotations

import logging
import time
from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .constants import DOMAIN, DEVICE_COORDINATOR, DATA_COORDINATOR
from .coordinator import HarviaDeviceCoordinator, HarviaDataCoordinator
from .api import HarviaDevice
from .device_info import build_device_info
from ._helpers import coerce_bool, data_attributes, latest_data

_LOGGER = logging.getLogger(__name__)


class HarviaOnOffEntity(CoordinatorEntity):
    """Base for Harvia on/off entities (sauna switch, fan, light).

    Fan/light (which set _data_key) are driven by the data coordinator; the sauna
    switch by the device coordinator. When optimistic mode is enabled, toggling
    flips the state instantly and reconciles once the eventually-consistent cloud
    catches up; a few forced fetches (bypassing the poll throttle) confirm it. The
    optimistic toggle and the forced-refresh timing come from the config options.
    Subclasses set _command and _data_key, or override _read_state.
    """

    _command: str = ""
    _data_key: Optional[str] = None

    def __init__(self, hass: HomeAssistant, entry_id: str, device: HarviaDevice) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._device = device
        self._optimistic: Optional[bool] = None
        self._optimistic_deadline = 0.0
        store = hass.data[DOMAIN][entry_id]
        self._optimistic_enabled = bool(store.get("optimistic", True))
        self._forced_delays = store.get("forced_delays", (10, 25))
        self._optimistic_timeout = float(store.get("optimistic_timeout", 30.0))
        super().__init__(store[DATA_COORDINATOR] if self._data_key else store[DEVICE_COORDINATOR])
        self._attr_device_info = build_device_info(device)

    @property
    def _device_coordinator(self) -> HarviaDeviceCoordinator:
        return self._hass.data[DOMAIN][self._entry_id][DEVICE_COORDINATOR]

    @property
    def _data_coordinator(self) -> HarviaDataCoordinator:
        return self._hass.data[DOMAIN][self._entry_id][DATA_COORDINATOR]

    def _read_state(self) -> Optional[bool]:
        """Real on/off from live telemetry; the sauna switch overrides this."""
        data = latest_data(self._data_coordinator, self._device.id)
        if not isinstance(data, dict):
            return None
        return coerce_bool(data.get(self._data_key))

    @property
    def is_on(self) -> Optional[bool]:
        if self._optimistic is not None:
            return self._optimistic
        return self._read_state()

    def _handle_coordinator_update(self) -> None:
        # Clear the optimistic override once the cloud confirms it, or on timeout.
        if self._optimistic is not None and (
            self._read_state() == self._optimistic
            or time.monotonic() >= self._optimistic_deadline
        ):
            self._optimistic = None
        super()._handle_coordinator_update()

    async def _async_set(self, on: bool) -> None:
        try:
            await self._device_coordinator.api.async_send_device_command(
                device_id=self._device.id,
                command=self._command,
                payload={"state": on, "cabin_id": "C1"},
            )
        except Exception:
            _LOGGER.exception(
                "Harvia %s command error device=%s", self._command, self._device.id
            )
            raise

        if self._optimistic_enabled:
            self._optimistic = on
            self._optimistic_deadline = time.monotonic() + self._optimistic_timeout
            self.async_write_ha_state()
        for delay in self._forced_delays:
            async_call_later(self._hass, delay, self._async_forced_refresh)

    async def _async_forced_refresh(self, _now=None) -> None:
        if self._optimistic_enabled and self._optimistic is None:
            return  # optimistic already confirmed; no need to keep forcing
        try:
            await self._device_coordinator.async_force_refresh()
            await self._data_coordinator.async_force_refresh()
        except Exception as err:
            _LOGGER.debug("Harvia forced refresh failed: %s", err)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return data_attributes(self._data_coordinator, self._device.id)


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
