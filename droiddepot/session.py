"""
DroidSession keeps a droid connected only while it is being used.

The first command connects (scanning for the droid by MAC address), every command
resets an idle timer, and when the timer runs out the session disconnects so the
droid's batteries aren't drained by a link nobody is using. Commands are serialized
per droid, so overlapping callers queue up instead of interleaving.

    session = DroidSession("AA:BB:CC:DD:EE:FF", idle_timeout=120)
    await session.run(lambda d: d.audio_controller.play_audio(bank_id=2))
    ...
    await session.close()
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional, TypeVar
from droiddepot.connection import DroidConnection, find_droid

logger = logging.getLogger(__name__)

T = TypeVar("T")

class DroidNotFoundError(ConnectionError):
    """
    Raised when the droid isn't advertising (powered off, out of range, or connected to something else).
    """

class DroidSession(object):
    """
    An on-demand connection to a single droid.

    Args:
        address (str): The droid's MAC address.
        idle_timeout (float): Seconds without a command before disconnecting. 0 or None keeps the link up.
        scan_timeout (float): Seconds to scan for the droid on each connect attempt.
        connect_attempts (int): Connect attempts per command before giving up.
        silent (bool): Skip the pairing chirp on connect and the shutdown sound on idle disconnect.
        on_state_change (callable): Called with True/False whenever the link goes up or down.
        finder (callable): Locates the droid and returns a DroidConnection. Overridable for tests.
    """

    def __init__(self, address: str, idle_timeout: Optional[float] = 120.0, scan_timeout: float = 15.0,
                 connect_attempts: int = 3, silent: bool = True,
                 on_state_change: Optional[Callable[[bool], None]] = None,
                 finder: Callable[..., Awaitable[Optional[DroidConnection]]] = find_droid) -> None:
        self.address = address
        self.idle_timeout = idle_timeout
        self.scan_timeout = scan_timeout
        self.connect_attempts = max(1, connect_attempts)
        self.silent = silent
        self.on_state_change = on_state_change
        self.finder = finder

        self.connection: Optional[DroidConnection] = None
        self._lock = asyncio.Lock()
        self._idle_task: Optional[asyncio.Task] = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """
        True while the droid is connected.
        """

        return self.connection is not None and self.connection.is_connected

    async def run(self, command: Callable[[DroidConnection], Awaitable[T]]) -> T:
        """
        Runs a command against the droid, connecting first if needed.

        Args:
            command: An async callable taking the DroidConnection, e.g. lambda d: d.motor_controller.center_head()

        Returns:
            Whatever the command returns.

        Raises:
            DroidNotFoundError: If the droid could not be found or connected to.
        """

        async with self._lock:
            self._cancel_idle_timer()
            try:
                await self._ensure_connected()
                return await command(self.connection)
            finally:
                self._start_idle_timer()

    async def connect(self) -> None:
        """
        Connects now without sending a command. The idle timer still applies.
        """

        async with self._lock:
            self._cancel_idle_timer()
            try:
                await self._ensure_connected()
            finally:
                self._start_idle_timer()

    async def disconnect(self, silent: Optional[bool] = None) -> None:
        """
        Disconnects now if connected.

        Args:
            silent (bool): Skip the shutdown sound. Defaults to the session's silent setting.
        """

        async with self._lock:
            self._cancel_idle_timer()
            await self._disconnect(self.silent if silent is None else silent)

    async def close(self) -> None:
        """
        Disconnects and stops the idle timer. Call on shutdown.
        """

        await self.disconnect()

    async def _ensure_connected(self) -> None:
        if self.is_connected:
            return

        last_error: Optional[BaseException] = None
        for attempt in range(1, self.connect_attempts + 1):
            connection = None
            try:
                connection = await self.finder(self.address, timeout=self.scan_timeout,
                                               disconnected_callback=self._on_disconnected)
                if connection is None:
                    raise DroidNotFoundError("Droid %s is not advertising (off, out of range, or connected elsewhere)" % self.address)

                await connection.connect(silent=self.silent)
                self.connection = connection
                self._set_state(True)
                return
            except Exception as err:
                last_error = err
                logger.warning("Connect to droid %s failed (attempt %d/%d): %s", self.address, attempt, self.connect_attempts, err)
                # Tear down a half-open link so the next attempt (and the droid) start clean.
                if connection is not None and connection.droid is not None:
                    try:
                        await connection.droid.disconnect()
                    except Exception:
                        pass
                if attempt < self.connect_attempts:
                    # BlueZ often reports "operation already in progress" right after a failure; give it a moment.
                    await asyncio.sleep(min(2 ** attempt, 10))

        raise DroidNotFoundError("Could not connect to droid %s: %s" % (self.address, last_error)) from last_error

    async def _disconnect(self, silent: bool) -> None:
        connection = self.connection
        self.connection = None
        if connection is not None:
            try:
                await connection.disconnect(silent=silent)
            except Exception as err:
                logger.warning("Disconnect from droid %s failed: %s", self.address, err)
        self._set_state(False)

    def _on_disconnected(self, connection: DroidConnection) -> None:
        # The link dropped underneath us (droid powered off, walked out of range, ...).
        if connection is self.connection:
            self._cancel_idle_timer()
            self._set_state(False)

    def _set_state(self, connected: bool) -> None:
        if connected == self._connected:
            return

        self._connected = connected
        if self.on_state_change is not None:
            try:
                self.on_state_change(connected)
            except Exception:
                logger.exception("on_state_change callback failed")

    def _start_idle_timer(self) -> None:
        if self.idle_timeout and self.is_connected:
            self._idle_task = asyncio.create_task(self._idle_disconnect())

    def _cancel_idle_timer(self) -> None:
        if self._idle_task is not None and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()
        self._idle_task = None

    async def _idle_disconnect(self) -> None:
        await asyncio.sleep(self.idle_timeout)
        async with self._lock:
            # A command may have run while we waited for the lock; it restarts its own timer.
            if self._idle_task is not asyncio.current_task():
                return
            self._idle_task = None
            logger.info("Droid %s idle for %ss, disconnecting", self.address, self.idle_timeout)
            await self._disconnect(self.silent)
