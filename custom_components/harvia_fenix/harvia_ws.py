from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.parse
import uuid

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# AppSync realtime subscription the MyHarvia app uses: pushes the full device
# shadow ('reported') on every change, keyed by receiver = deviceId.
_SUBSCRIPTION = (
    "subscription S($receiver: ID!) { devicesStatesUpdateFeed(receiver: $receiver) "
    "{ receiver item { deviceId shadowName reported connectionState { connected } } } }"
)

_MAX_BACKOFF = 60


class HarviaWebsocket:
    """Realtime device-state feed over AppSync websockets.

    Subscribes to devicesStatesUpdateFeed for each device and pushes the
    normalized state into the device coordinator, so entities update instantly
    instead of waiting for the poll. Reconnects with backoff; refreshes the token
    on each (re)connect.
    """

    def __init__(self, hass: HomeAssistant, api, device_coordinator) -> None:
        self._hass = hass
        self._api = api
        self._coordinator = device_coordinator
        self._task: asyncio.Task | None = None
        self._closing = False

    def start(self) -> None:
        self._task = self._hass.async_create_background_task(self._runner(), "harvia_ws")

    async def stop(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _runner(self) -> None:
        backoff = 1
        while not self._closing:
            try:
                await self._connect_once()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep the loop alive
                _LOGGER.warning("Harvia websocket error: %s (reconnect in %ss)", err, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    def _connect_url(self, token: str, host: str) -> str:
        header = base64.b64encode(
            json.dumps({"Authorization": f"Bearer {token}", "host": host}).encode()
        ).decode()
        return f"{self._api.graphql_device_wss}?header={urllib.parse.quote(header)}&payload=e30="

    async def _connect_once(self) -> None:
        host = self._api.graphql_device_host
        if not self._api.graphql_device_wss or not host:
            raise RuntimeError("Harvia GraphQL websocket endpoint not initialized")

        token = await self._api.async_valid_id_token()
        authz = {"Authorization": f"Bearer {token}", "host": host}
        session = async_get_clientsession(self._hass)

        async with session.ws_connect(
            self._connect_url(token, host), protocols=("graphql-ws",)
        ) as ws:
            await ws.send_json({"type": "connection_init"})
            subscribed = False

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                mtype = data.get("type")

                if mtype == "connection_ack":
                    devices = (self._coordinator.data or {}).get("devices", [])
                    for dev in devices:
                        await ws.send_json({
                            "id": str(uuid.uuid4()),
                            "type": "start",
                            "payload": {
                                "data": json.dumps(
                                    {"query": _SUBSCRIPTION, "variables": {"receiver": dev.id}}
                                ),
                                "extensions": {"authorization": authz},
                            },
                        })
                    subscribed = True
                    _LOGGER.info("Harvia websocket subscribed to %d device(s)", len(devices))
                elif mtype == "data":
                    self._on_push(data)
                elif mtype == "error":
                    raise RuntimeError(f"subscription error: {data.get('payload')}")
                # 'ka' (keepalive), 'start_ack', 'complete' need no action

            if not subscribed:
                raise RuntimeError("websocket closed before connection_ack")

    def _on_push(self, data: dict) -> None:
        try:
            item = data["payload"]["data"]["devicesStatesUpdateFeed"]["item"]
            device_id = item["deviceId"]
            reported = json.loads(item["reported"])
        except (KeyError, TypeError, ValueError):
            _LOGGER.debug("Harvia websocket: unparseable push: %s", str(data)[:200])
            return
        normalized = self._api.normalize_reported(reported)
        self._coordinator.apply_pushed_state(device_id, normalized)
