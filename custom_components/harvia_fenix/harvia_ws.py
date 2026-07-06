from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.parse
import uuid

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HarviaAuthError

_LOGGER = logging.getLogger(__name__)

# AppSync realtime subscription the MyHarvia app uses: pushes the full device
# shadow ('reported') on every change, keyed by receiver = deviceId.
_SUBSCRIPTION = (
    "subscription S($receiver: ID!) { devicesStatesUpdateFeed(receiver: $receiver) "
    "{ receiver item { deviceId shadowName reported connectionState { connected } } } }"
)

_MAX_BACKOFF = 60          # cap between reconnect attempts (s)
_ACK_TIMEOUT = 15          # wait this long for connection_ack (s)
_HEARTBEAT = 30            # aiohttp ws ping interval — detects a dead peer (s)
_STABLE_SECONDS = 30       # a connection that lasted this long resets the backoff


class HarviaWebsocket:
    """Realtime device-state feed over AppSync websockets.

    Subscribes to devicesStatesUpdateFeed for each device and pushes the
    normalized state into the device coordinator, so entities update instantly
    instead of waiting for the poll. Reconnects with backoff; refreshes the token
    on each (re)connect.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, api, device_coordinator) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._api = api
        self._coordinator = device_coordinator
        self._task: asyncio.Task | None = None
        self._closing = False

    def start(self) -> None:
        self._task = self._hass.async_create_background_task(
            self._runner(), f"harvia_ws_{self._entry_id}"
        )

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
            started = time.monotonic()
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except HarviaAuthError as err:
                # Don't hammer the login endpoint on bad credentials; the device
                # coordinator escalates reauth, which reloads the entry and
                # restarts this task.
                _LOGGER.error("Harvia websocket auth failed; stopping: %s", err)
                return
            except Exception as err:  # noqa: BLE001 - keep the loop alive
                _LOGGER.warning("Harvia websocket error: %s", err)
            if self._closing:
                break
            # Reset backoff only after a stable connection; a quick failure grows
            # it so a flapping server isn't hammered (and re-auth isn't spammed).
            if time.monotonic() - started >= _STABLE_SECONDS:
                backoff = 1
            else:
                backoff = min(backoff * 2, _MAX_BACKOFF)
            _LOGGER.debug("Harvia websocket reconnecting in %ss", backoff)
            await asyncio.sleep(backoff)

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
            self._connect_url(token, host),
            protocols=("graphql-ws",),
            heartbeat=_HEARTBEAT,
        ) as ws:
            await ws.send_json({"type": "connection_init"})
            # Bounded wait for the ack so a silent server can't hang us forever.
            ack = await asyncio.wait_for(ws.receive_json(), timeout=_ACK_TIMEOUT)
            if ack.get("type") != "connection_ack":
                raise RuntimeError(f"unexpected first frame: {ack}")

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
            _LOGGER.info("Harvia websocket subscribed to %d device(s)", len(devices))

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                mtype = data.get("type")
                if mtype == "data":
                    self._on_push(data)
                elif mtype == "error":
                    raise RuntimeError(f"subscription error: {data.get('payload')}")
                # 'ka' (keepalive), 'start_ack', 'complete' need no action

    def _on_push(self, data: dict) -> None:
        # A single bad frame must not tear down the subscription, so guard the
        # whole parse-normalize-apply path.
        try:
            item = data["payload"]["data"]["devicesStatesUpdateFeed"]["item"]
            device_id = item["deviceId"]
            reported = json.loads(item["reported"])
            connection = item.get("connectionState")
            normalized = self._api.normalize_reported(reported, connection)
            self._coordinator.apply_pushed_state(device_id, normalized)
        except Exception as err:  # noqa: BLE001 - one bad push shouldn't kill the stream
            _LOGGER.debug("Harvia websocket: bad push (%s): %s", err, str(data)[:200])
