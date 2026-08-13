"""WebSocket push-channel client with reconnect/backoff.

The WS is an accelerator, not the source of truth — every event is recoverable
via REST (GET /workers/{id}/state). This client reconnects with exponential
backoff and surfaces frames to the agent via an async callback.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import websockets

logger = logging.getLogger(__name__)

FrameHandler = Callable[[dict], Awaitable[None]]


class WsClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        on_frame: FrameHandler,
        backoff: list[int] | None = None,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.on_frame = on_frame
        self.backoff = backoff or [1, 30]
        self._stop = asyncio.Event()
        self._ws: websockets.ClientConnection | None = None

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, additional_headers={"Authorization": f"Bearer {self.api_key}"}) as ws:
                    logger.info("WS connected: %s", self.url)
                    attempt = 0
                    self._ws = ws
                    async for raw in ws:
                        try:
                            frame = json.loads(raw)
                        except ValueError:
                            continue
                        try:
                            await self.on_frame(frame)
                        except Exception as exc:  # noqa: BLE001
                            # A handler error must not kill the WS connection.
                            logger.error("WS frame handler error: %s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("WS error: %s", exc)
            finally:
                self._ws = None
            if self._stop.is_set():
                break
            delay = self.backoff[min(attempt, len(self.backoff) - 1)]
            attempt += 1
            logger.info("WS reconnect in %ss (attempt %d)", delay, attempt)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def send_frame(self, frame: dict) -> bool:
        """Send a frame to the server if connected. Returns True if sent."""
        ws = self._ws
        if ws is None:
            logger.warning("WS not connected; dropping frame %s", frame.get("type"))
            return False
        try:
            await ws.send(json.dumps(frame))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("WS send failed: %s", exc)
            return False

    def stop(self) -> None:
        self._stop.set()
