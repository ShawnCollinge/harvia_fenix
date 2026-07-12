from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HarviaSaunaAPI, HarviaAuthError
from .constants import (
    CONF_DATA_POLL_INTERVAL,
    CONF_DEVICE_POLL_INTERVAL,
    POLL_INTERVAL_OPTIONS,
    DEFAULT_DATA_POLL_LABEL,
    DEFAULT_DEVICE_POLL_LABEL,
)

_LOGGER = logging.getLogger(__name__)


def _parse_interval(value: Any, default_label: str) -> int:
    """Accept either label ('30s') or int seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return int(POLL_INTERVAL_OPTIONS.get(value, POLL_INTERVAL_OPTIONS[default_label]))
    return int(POLL_INTERVAL_OPTIONS[default_label])


FIXED_TICK_SECONDS = 30  # coordinator ticks every 30s; the real fetch is throttled below
PUSH_FRESH_SECONDS = 30  # a push newer than this outranks an eventually-consistent shadow read


class HarviaDeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for devices + state (slower)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: HarviaSaunaAPI) -> None:
        self.api = api

        self._device_interval = _parse_interval(
            entry.options.get(CONF_DEVICE_POLL_INTERVAL, DEFAULT_DEVICE_POLL_LABEL),
            DEFAULT_DEVICE_POLL_LABEL,
        )

        super().__init__(
            hass,
            _LOGGER,
            name="harvia_fenix_device",
            update_interval=timedelta(seconds=FIXED_TICK_SECONDS),
        )

        self._last_device_refresh: float = 0.0
        self._devices: list[Any] = []
        self._states: dict[str, Any] = {}
        self._last_push: dict[str, float] = {}

        _LOGGER.info(
            "Harvia device/state polling configured: tick=%ss device/state=%ss",
            FIXED_TICK_SECONDS,
            self._device_interval,
        )

    def apply_pushed_state(self, device_id: str, normalized: dict[str, Any]) -> None:
        """Merge a websocket-pushed device state and notify entities immediately."""
        prev = self._states.get(device_id)
        if isinstance(prev, dict):
            # If a push ever arrives as a partial document, absent keys
            # normalize to None — keep the last known value rather than wipe
            # every other entity to unknown.
            normalized = {
                k: (prev.get(k) if v is None else v) for k, v in normalized.items()
            }
        self._last_push[device_id] = time.monotonic()
        self._states[device_id] = normalized
        self.async_set_updated_data({"devices": self._devices, "states": self._states})

    async def async_force_refresh(self) -> None:
        """Refresh devices/state now, bypassing the poll-interval throttle."""
        self._last_device_refresh = 0.0
        await self.async_request_refresh()

    async def _async_update_data(self) -> dict[str, Any]:
        now = time.monotonic()

        try:
            if (not self._devices) or (now - self._last_device_refresh) >= self._device_interval:
                _LOGGER.debug("Harvia: refreshing devices/state (interval=%ss)", self._device_interval)
                self._devices = await self.api.get_devices()
                for dev in self._devices:
                    # The REST shadow read is eventually consistent and can lag
                    # a recent command by many seconds; a live push is always
                    # newer, so never let a poll overwrite one.
                    fetch_start = time.monotonic()
                    if fetch_start - self._last_push.get(dev.id, float("-inf")) < PUSH_FRESH_SECONDS:
                        continue
                    state = await self.api.refresh_device_state(dev)
                    if self._last_push.get(dev.id, float("-inf")) >= fetch_start:
                        continue  # a push landed mid-fetch and is newer
                    self._states[dev.id] = state
                self._last_device_refresh = now
            else:
                _LOGGER.debug("Harvia: skipping devices/state (cached)")

            return {
                "devices": self._devices,
                "states": self._states,
            }

        except HarviaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"Harvia device/state update failed: {err}") from err


class HarviaDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for latest-data telemetry (fast). Uses devices from HarviaDeviceCoordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: HarviaSaunaAPI,
        device_coordinator: HarviaDeviceCoordinator,
    ) -> None:
        self.api = api
        self._device_coordinator = device_coordinator

        self._data_interval = _parse_interval(
            entry.options.get(CONF_DATA_POLL_INTERVAL, DEFAULT_DATA_POLL_LABEL),
            DEFAULT_DATA_POLL_LABEL,
        )

        super().__init__(
            hass,
            _LOGGER,
            name="harvia_fenix_data",
            update_interval=timedelta(seconds=FIXED_TICK_SECONDS),
        )

        self._last_data_refresh: float = 0.0
        self._latest_data: dict[str, Any] = {}

        _LOGGER.info(
            "Harvia latest-data polling configured: tick=%ss data=%ss",
            FIXED_TICK_SECONDS,
            self._data_interval,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        now = time.monotonic()

        try:
            if (now - self._last_data_refresh) >= self._data_interval:
                _LOGGER.debug("Harvia: refreshing latest-data (interval=%ss)", self._data_interval)

                devices: list[Any] = self._device_coordinator.data.get("devices", []) if self._device_coordinator.data else []
                if not devices:
                    # If the device coordinator has no data yet, kick it once
                    await self._device_coordinator.async_request_refresh()
                    devices = self._device_coordinator.data.get("devices", []) if self._device_coordinator.data else []

                for dev in devices:
                    try:
                        self._latest_data[dev.id] = await self.api.get_latest_data(dev)
                    except Exception as err:
                        _LOGGER.debug("Harvia latest-data failed for %s: %s", dev.id, err)

                self._last_data_refresh = now
            else:
                _LOGGER.debug("Harvia: skipping latest-data (cached)")

            return {
                "latest_data": self._latest_data,
            }

        except HarviaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"Harvia latest-data update failed: {err}") from err
