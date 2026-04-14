"""Station-side WebSocket client (Phase 2).

Runs in a background thread with its own asyncio loop so the main station
cycle loop (station.py is synchronous) is never blocked by network I/O (D-07, PITFALLS #7/#10).

Public API:
    build_status_listener(config) -> Callable[[StationState], None]
    StationWsClient(url, station_id)
"""
from __future__ import annotations
import asyncio
import logging
import threading
from typing import Callable, Optional

from ws_protocol import encode, RegisterMsg, state_to_status_msg

log = logging.getLogger(__name__)

_BACKOFF_SCHEDULE = [1.0, 2.0, 4.0, 8.0]  # D-08


class StationWsClient:
    def __init__(self, url: str, station_id: str):
        self.url = url if "?" in url else (url + "?role=station")
        self.station_id = station_id
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._pending_msg: Optional[str] = None   # coalesced latest StatusMsg JSON
        self._last_state = None                    # for replay on reconnect (D-09)
        self._state_event: Optional[asyncio.Event] = None
        self._stop_flag = threading.Event()
        self._started = False

    def start_in_thread(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run_loop, name="ws-client", daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._state_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            log.warning("ws_client loop exited: %s", e)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def enqueue_state(self, state) -> None:
        """Called from the station thread. Coalesces to latest state only."""
        if self._loop is None or self._state_event is None:
            return
        msg_json = encode(state_to_status_msg(state))

        def _put():
            self._last_state = state
            self._pending_msg = msg_json
            self._state_event.set()

        try:
            self._loop.call_soon_threadsafe(_put)
        except RuntimeError:
            # loop closed
            pass

    def stop(self) -> None:
        self._stop_flag.set()
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._state_event.set)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    async def _main(self) -> None:
        attempt = 0
        while not self._stop_flag.is_set():
            try:
                await self._connect_and_pump()
                attempt = 0   # reset on clean disconnect
            except Exception as e:
                log.info("ws_client connect/pump failed: %s", e)
            if self._stop_flag.is_set():
                break
            delay = _BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)]
            attempt += 1
            log.info("ws_client reconnecting in %.1fs", delay)
            await asyncio.sleep(delay)

    async def _connect_and_pump(self) -> None:
        import websockets   # imported lazily so tests can avoid the dep
        async with websockets.connect(self.url) as ws:
            log.info("ws_client connected to %s", self.url)
            await ws.send(encode(RegisterMsg(station_id=self.station_id)))
            # D-09 replay current state on (re)connect
            if self._last_state is not None:
                await ws.send(encode(state_to_status_msg(self._last_state)))
                self._pending_msg = None
            while not self._stop_flag.is_set():
                await self._state_event.wait()
                self._state_event.clear()
                if self._stop_flag.is_set():
                    break
                if self._pending_msg is not None:
                    payload = self._pending_msg
                    self._pending_msg = None
                    await ws.send(payload)


def build_status_listener(config) -> Callable:
    """Return a callable suitable for Station(status_listener=...).

    If config.orchestrator_enabled is False (D-06 / ORC-04), returns a no-op.
    Otherwise returns a listener that lazily starts a StationWsClient thread.
    """
    if not getattr(config, "orchestrator_enabled", False):
        log.info("ws_client disabled (orchestrator_url empty) — standalone mode")

        def _noop(_state):
            return None

        return _noop

    client = StationWsClient(url=config.orchestrator_url, station_id=config.station_id)

    def _listener(state):
        if not client._started:
            client.start_in_thread()
        client.enqueue_state(state)

    _listener._client = client   # test hook
    return _listener
