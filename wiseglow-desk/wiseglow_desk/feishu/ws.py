"""Feishu long connection (WebSocket event subscription) in a background thread.

``lark.ws.Client.start()`` blocks forever and drives a module-level asyncio
loop of its own, so it must live in a dedicated daemon thread. The event
handler runs *synchronously inside that loop* and the SDK acks the event
only after it returns (a raised exception yields a 500 and Feishu retries),
so ``on_event`` must return within a few hundred milliseconds: typically it
hands the event to the bot's asyncio loop with ``loop.call_soon_threadsafe``
and returns.

``start()`` only returns or raises when the *initial* connection fails.
Once connected it sleeps forever in the SDK loop while a background task
handles reconnection, and when that task gives up (server-capped retry
budget, 403, rotated app secret) the client is silently deaf with ``start()``
still blocked. A small watchdog thread therefore polls the client's
connection state and, when it has been down for longer than ``dead_after``,
cancels every task on the SDK loop: ``start()`` then raises
``CancelledError`` and the restart loop builds a fresh client. ``stop()``
uses the same cancellation to end the thread cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.ws import client as sdk_ws

from .client import resolve_domain

log = logging.getLogger(__name__)

RESTART_DELAY_S = 10.0
WATCHDOG_INTERVAL_S = 15.0
DEAD_AFTER_S = 600.0

_UNKNOWN = object()  # sentinel: the client does not expose its connection state


class LongConnection:
    """Run the SDK long connection in a daemon thread and restart it on exit."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        domain: str,
        on_event: Callable[[P2ImMessageReceiveV1], None],
        *,
        client_factory: Callable[..., Any] | None = None,
        sdk_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = resolve_domain(domain)
        self._on_event = on_event
        self._client_factory = client_factory or lark.ws.Client
        self._sdk_loop = sdk_loop
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.restart_delay = RESTART_DELAY_S
        self.watchdog_interval = WATCHDOG_INTERVAL_S
        self.dead_after = DEAD_AFTER_S
        self.restarts = 0
        self.watchdog_kicks = 0

    # -- public API ----------------------------------------------------------

    def build_handler(self) -> lark.EventDispatcherHandler:
        """Dispatcher with our ``im.message.receive_v1`` processor registered."""
        return lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(self._dispatch).build()

    def start_in_thread(self) -> threading.Thread:
        """Start (or return the already running) daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        thread = threading.Thread(target=self._run_forever, name="feishu-ws", daemon=True)
        thread.start()
        self._thread = thread
        return thread

    def stop(self) -> None:
        """Ask the loop not to restart and unblock ``start()`` so the thread ends."""
        self._stop.set()
        self._cancel_sdk_loop()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def sdk_loop(self) -> asyncio.AbstractEventLoop:
        """The loop ``client.start()`` blocks in (the SDK's module-level one by default)."""
        return self._sdk_loop if self._sdk_loop is not None else sdk_ws.loop

    # -- internals -----------------------------------------------------------

    def _dispatch(self, event: P2ImMessageReceiveV1) -> None:
        """Called by the SDK inside its loop: forward and never raise."""
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 - a raise would make Feishu redeliver
            log.exception("Feishu event handler failed")

    def _make_client(self) -> Any:
        return self._client_factory(
            self._app_id,
            self._app_secret,
            event_handler=self.build_handler(),
            log_level=lark.LogLevel.INFO,
            domain=self._domain,
        )

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            client = self._make_client()
            watchdog_done = threading.Event()
            threading.Thread(
                target=self._watchdog, args=(client, watchdog_done), name="feishu-ws-watchdog", daemon=True
            ).start()
            try:
                log.info("Feishu long connection starting (domain=%s)", self._domain)
                client.start()
                log.warning("Feishu long connection returned")
            except asyncio.CancelledError:
                log.warning("Feishu long connection interrupted (watchdog or stop)")
            except Exception:  # noqa: BLE001
                log.exception("Feishu long connection failed")
            finally:
                watchdog_done.set()
            if self._stop.wait(self.restart_delay):
                break
            self.restarts += 1
            log.info("Feishu long connection restart #%d", self.restarts)
        log.info("Feishu long connection thread exiting")

    def _watchdog(self, client: Any, done: threading.Event) -> None:
        """Kick the SDK loop when ``client`` has had no connection for ``dead_after`` seconds.

        Runs in its own thread until ``done`` is set (i.e. ``start()`` returned).
        The kick is repeated every interval while the outage lasts, so a kick
        that finds the loop momentarily not running is retried.
        """
        down_since: float | None = None
        while not done.wait(self.watchdog_interval):
            if getattr(client, "_conn", _UNKNOWN) is not None:
                down_since = None
                continue
            now = time.monotonic()
            down_since = now if down_since is None else down_since
            if now - down_since < self.dead_after:
                continue
            self.watchdog_kicks += 1
            log.error(
                "Feishu long connection down for %.0fs (watchdog kick #%d): forcing a restart",
                now - down_since, self.watchdog_kicks,
            )
            self._cancel_sdk_loop()

    def _cancel_sdk_loop(self) -> None:
        """Cancel every task on the SDK loop (from any thread) so ``start()`` raises CancelledError."""
        loop = self.sdk_loop
        if loop.is_closed() or not loop.is_running():
            return  # nothing is blocked in it right now

        def cancel_all() -> None:
            for task in asyncio.all_tasks(loop):
                task.cancel()

        try:
            loop.call_soon_threadsafe(cancel_all)
        except RuntimeError as exc:  # loop closed in between
            log.debug("SDK loop non cancellabile: %s", exc)
