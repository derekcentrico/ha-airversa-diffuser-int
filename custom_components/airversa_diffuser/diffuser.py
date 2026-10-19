"""Persistent BLE connection manager for an Airversa Scenta diffuser.

Goes through Home Assistant's Bluetooth stack so the link can be routed via
ESPHome or Shelly proxies:

* ``bluetooth.async_ble_device_from_address(..., connectable=True)`` returns a
  ``BLEDevice`` that may live behind a proxy.
* ``bleak_retry_connector.establish_connection`` picks the adapter or proxy
  with the best path to the device.

Frame building and parsing live in ``protocol.py``. This module owns the
connection lifecycle, command/ACK correlation and the state exposed to
entities.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from . import protocol as p
from .const import (
    ACK_TIMEOUT,
    COMMAND_ATTEMPTS,
    DEVICE_WAIT_TIMEOUT,
    HANDSHAKE_SETTLE,
    KEEPALIVE_SETTLE,
    LOGIN_TIMEOUT,
    PIN_REJECTED_DELAY,
    QUEUE_TTL,
    RECONNECT_DELAY,
    SYNC_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

SOURCE_DEVICE = "device"
SOURCE_COMMAND = "command"
# Device-reported state from an earlier session, not yet refreshed.
SOURCE_PREVIOUS = "previous_session"

# Pushes that may carry user-chosen text; logged by head only.
_PRIVATE_PUSHES = frozenset({p.PUSH_NAME, p.PUSH_LABEL})


class DiffuserError(Exception):
    """Raised when a command cannot be delivered or confirmed."""


class PinRejectedError(DiffuserError):
    """Raised when the device answers the login with ERROR."""


@dataclass(slots=True)
class _Pending:
    head: int
    confirm_pushes: frozenset[int]
    future: asyncio.Future[bytes]
    # Checked against the raw push after it is applied; a push that does
    # not show the requested change (a button press, say) is not a reply.
    matches: Callable[[bytes], bool] | None = None


class AirversaDiffuser:
    """Maintains one always-on BLE connection to a single diffuser."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        keepalive: float,
        *,
        pin: str = p.DEFAULT_PIN,
        model: str | None = None,
        sync_state: bool = True,
        advertised_firmware: str | None = None,
    ) -> None:
        """Initialise the connection manager."""
        self.hass = hass
        self.address = address.upper()
        self.keepalive = keepalive
        self.pin = pin
        self.sync_state = sync_state
        self.snapshot = p.DeviceSnapshot(model=model)
        # BLE module string from the login reply, e.g. "CY_V3.0".
        self.firmware: str | None = None
        # Main firmware version from the advertisement, e.g. "11.1.2".
        self.advertised_firmware = advertised_firmware

        self._client: BleakClientWithServiceCache | None = None
        self._run_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._cmd_lock = asyncio.Lock()
        # Power and mute share one byte; this lock covers reading the
        # current pair and writing the new one.
        self._power_lock = asyncio.Lock()
        # Every GATT write goes through one queue, like the vendor app.
        self._write_lock = asyncio.Lock()
        self._pending: _Pending | None = None
        self._login_future: asyncio.Future[bytes] | None = None
        self._sync_done = asyncio.Event()
        self._background: set[asyncio.Task] = set()

        self._connected = False
        self._power: bool | None = None
        self._power_source: str | None = None
        self._muted: bool | None = None
        self._muted_source: str | None = None
        # Power requested while disconnected; sent once the link is up
        # unless it has gone stale.
        self._queued_power: bool | None = None
        self._queued_at = 0.0
        # Last requested power, re-sent on reconnect only for devices that
        # never report their own state (pre-existing behaviour).
        self._desired: bool | None = None
        # Set once the device has pushed 0x4d; a later sync timeout is then
        # not a reason to fall back to re-applying the last request.
        self._reports_power = False
        self.last_error: str | None = None

        self._listeners: set[CALLBACK_TYPE] = set()
        self._model_listeners: set[Callable[[str], None]] = set()

    # ------------------------------------------------------------------ state
    @property
    def connected(self) -> bool:
        """Return True while a live, logged-in BLE link is held."""
        return self._connected

    @property
    def model(self) -> str | None:
        """Return the model code (ANP, ANA, ...) if known."""
        return self.snapshot.model

    @property
    def is_on(self) -> bool | None:
        """Return the best known power state (None if unknown)."""
        return self._power

    @property
    def power_source(self) -> str | None:
        """Return where the power state came from: device or command."""
        return self._power_source

    @property
    def power_reported(self) -> bool:
        """Return True when the device itself reported the power state."""
        return self._power_source == SOURCE_DEVICE

    @property
    def power_fresh(self) -> bool:
        """Return True when power is known from this session."""
        return self._power is not None and self._power_source in (
            SOURCE_DEVICE,
            SOURCE_COMMAND,
        )

    @property
    def muted(self) -> bool | None:
        """Return the feedback-sound mute flag if known."""
        return self._muted

    @property
    def muted_reported(self) -> bool:
        """Return True when the device itself reported the mute flag."""
        return self._muted_source == SOURCE_DEVICE

    @callback
    def add_listener(
        self, update_callback: CALLBACK_TYPE
    ) -> Callable[[], None]:
        """Register an entity callback fired when state changes."""
        self._listeners.add(update_callback)

        def _remove() -> None:
            self._listeners.discard(update_callback)

        return _remove

    @callback
    def add_model_listener(
        self, model_callback: Callable[[str], None]
    ) -> Callable[[], None]:
        """Register a callback fired when the device reports its model."""
        self._model_listeners.add(model_callback)

        def _remove() -> None:
            self._model_listeners.discard(model_callback)

        return _remove

    @callback
    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    # ------------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        """Start the background connection task."""
        self._stop.clear()
        self._run_task = self.hass.async_create_background_task(
            self._run(), name=f"airversa_diffuser[{self.address}]"
        )

    async def async_stop(self) -> None:
        """Stop the background task and tear down any live connection."""
        self._stop.set()
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None
        for task in list(self._background):
            task.cancel()
        await self._async_disconnect()

    async def _async_disconnect(self) -> None:
        client = self._client
        self._client = None
        self._connected = False
        self._fail_pending("disconnected")
        if client is not None:
            try:
                await client.disconnect()
            except BleakError as err:
                _LOGGER.debug(
                    "%s: error during disconnect: %s", self.address, err
                )
        self._notify_listeners()

    @callback
    def _fail_pending(self, reason: str) -> None:
        pending = self._pending
        self._pending = None
        if pending is not None and not pending.future.done():
            pending.future.set_exception(DiffuserError(reason))
            # Mark retrieved so a waiter-less drain sink does not warn.
            pending.future.exception()

    async def _async_write(
        self, client: BleakClientWithServiceCache, frame: bytes
    ) -> None:
        async with self._write_lock:
            await client.write_gatt_char(p.CHAR_UUID, frame, response=False)

    # -------------------------------------------------------------- commands
    async def async_turn_on(self) -> None:
        """Request the diffuser turn on."""
        await self._async_set_power(True)

    async def async_turn_off(self) -> None:
        """Request the diffuser turn off."""
        await self._async_set_power(False)

    async def _async_set_power(self, on: bool) -> None:
        if not self._link_ready():
            self._queued_power = on
            self._queued_at = time.monotonic()
            _LOGGER.debug(
                "%s: queued %s (not connected yet)",
                self.address,
                "ON" if on else "OFF",
            )
            return
        # A direct command supersedes anything queued while offline.
        self._queued_power = None
        async with self._power_lock:
            await self._async_send_power(on, self._mute_for_write())
        self._desired = on

    async def async_set_sound(self, enabled: bool) -> None:
        """Enable or disable the feedback beep.

        Mute shares a byte with power, so the current power state has to be
        known or the write would also switch the diffuser.
        """
        async with self._power_lock:
            if not self.power_fresh:
                raise DiffuserError(
                    "power state not confirmed this session; "
                    "cannot change sound safely"
                )
            self._require_link()
            await self._async_send_power(bool(self._power), not enabled)

    async def _async_send_power(self, on: bool, muted: bool) -> None:
        frame = p.build_power(on, muted)
        control_before = self.snapshot.control

        def applied(_push: bytes) -> bool:
            control = self.snapshot.control
            return (
                control is not None
                and control.power == on
                and control.muted == muted
            )

        ack = await self._async_command(
            frame, confirm_pushes=frozenset({p.PUSH_CONTROL}), matches=applied
        )
        if ack is None:
            raise DiffuserError(
                f"no acknowledgement for {'ON' if on else 'OFF'}; "
                "state left unchanged"
            )
        if ack[0] == p.PUSH_CONTROL or self.snapshot.control is not (
            control_before
        ):
            # The device reported its real state while the command was in
            # flight; that already applied and wins over the request.
            return
        if len(ack) == 2 and ack[1] & 0x12 == 0x12:
            # Older firmware echoes the value it applied; trust that over
            # what was requested.
            on = bool(ack[1] & 0x01)
            muted = bool(ack[1] & 0x40)
        self._power = on
        self._power_source = SOURCE_COMMAND
        self._muted = muted
        self._muted_source = SOURCE_COMMAND
        _LOGGER.debug(
            "%s: %s acknowledged (%s)", self.address, frame.hex(), ack.hex()
        )
        self._notify_listeners()

    def _mute_for_write(self) -> bool:
        # Unknown mute state falls back to "sound on", which is what every
        # earlier release of this integration always sent.
        return bool(self._muted)

    async def async_set_grade(self, grade: int) -> None:
        """Select a preset intensity level. Switches the device to preset mode."""
        mode = self._require_mode()
        max_grade = self.max_grade
        if not 1 <= grade <= max_grade:
            raise DiffuserError(f"intensity must be 1-{max_grade}")
        await self._async_send_mode(
            p.Mode.PRESET, mode.work_mode, grade, mode.on_time, mode.off_time
        )

    async def async_set_mode(self, mode_value: int) -> None:
        """Switch the operating mode (ANP)."""
        mode = self._require_mode()
        await self._async_send_mode(
            mode_value, mode.work_mode, mode.grade, mode.on_time, mode.off_time
        )

    async def _async_send_mode(
        self,
        mode_value: int,
        work_mode: int,
        grade: int,
        on_time: int | None,
        off_time: int | None,
    ) -> None:
        self._require_link()
        frame = p.build_mode(
            int(mode_value), work_mode, grade, on_time or 0, off_time or 0
        )

        def applied(_push: bytes) -> bool:
            state = self.snapshot.mode
            return (
                state is not None
                and state.mode == int(mode_value)
                and (state.mode != p.Mode.PRESET or state.grade == grade)
            )

        ack = await self._async_command(
            frame, confirm_pushes=frozenset({p.PUSH_MODE}), matches=applied
        )
        if ack is None:
            raise DiffuserError("device did not confirm the mode change")
        if ack[0] != p.PUSH_MODE and self.snapshot.mode is not None:
            # ACK only: reflect the request until the device pushes state.
            self.snapshot.mode.mode = int(mode_value)
            self.snapshot.mode.grade = grade
            self._notify_listeners()

    async def async_set_schedule(self, schedule: p.Schedule) -> None:
        """Write one native schedule slot."""
        self._require_link()
        custom = bool(
            self.snapshot.login and self.snapshot.login.custom_timing
        )
        frame = p.build_schedule(schedule, custom)
        ack = await self._async_command(
            frame,
            confirm_pushes=frozenset({p.PUSH_SCHEDULE}),
            matches=lambda push: push[5] == schedule.index,
        )
        if ack is None:
            raise DiffuserError("device did not confirm the schedule write")
        if ack[0] != p.PUSH_SCHEDULE:
            self.snapshot.schedules[schedule.index] = schedule
            self._notify_listeners()

    @property
    def max_grade(self) -> int:
        """Return the number of preset intensity levels."""
        if self.snapshot.limits and self.snapshot.limits.max_grade > 0:
            return self.snapshot.limits.max_grade
        if self.snapshot.grades:
            return len(self.snapshot.grades)
        return 4

    def _require_mode(self) -> p.ModeState:
        if self.snapshot.mode is None:
            raise DiffuserError("mode state not received from the device yet")
        return self.snapshot.mode

    def _link_ready(self) -> bool:
        client = self._client
        return self._connected and client is not None and client.is_connected

    def _require_link(self) -> None:
        if not self._link_ready():
            raise DiffuserError("not connected")

    async def _async_command(
        self,
        frame: bytes,
        *,
        confirm_pushes: frozenset[int] = frozenset(),
        matches: Callable[[bytes], bool] | None = None,
        attempts: int | None = None,
        timeout: float | None = None,
    ) -> bytes | None:
        """Write ``frame`` and wait for its ACK or a confirming push.

        Commands are serialised because newer firmware acks with a single byte
        that only identifies the command type, not the request.
        """
        attempts = attempts or COMMAND_ATTEMPTS
        timeout = timeout or ACK_TIMEOUT
        async with self._cmd_lock:
            loop = asyncio.get_running_loop()
            for attempt in range(1, attempts + 1):
                client = self._client
                if client is None or not client.is_connected:
                    raise DiffuserError("not connected")
                future: asyncio.Future[bytes] = loop.create_future()
                self._pending = _Pending(
                    frame[0], confirm_pushes, future, matches
                )
                try:
                    _LOGGER.debug("%s: tx %s", self.address, frame.hex())
                    await self._async_write(client, frame)
                    reply = await asyncio.wait_for(future, timeout)
                except TimeoutError:
                    _LOGGER.debug(
                        "%s: no ACK for %s (attempt %d/%d)",
                        self.address,
                        frame[:1].hex(),
                        attempt,
                        attempts,
                    )
                except BleakError as err:
                    raise DiffuserError(f"write failed: {err}") from err
                else:
                    self._pending = None
                    if not reply[0] & 0x80:
                        # Confirmed by a push; the real ACK may still be on
                        # its way and must not confirm the next command.
                        await self._async_drain_late_ack(frame[0], timeout)
                    return reply
                finally:
                    self._pending = None
            await self._async_drain_late_ack(frame[0], timeout)
            return None

    async def _async_drain_late_ack(self, head: int, timeout: float) -> None:
        """Swallow a late ACK so it cannot confirm the next same-type command.

        Single-byte ACKs carry no request id. Holding the lock for one more
        window after a failed command keeps a straggler from the last retry
        away from whatever is queued behind it.
        """
        loop = asyncio.get_running_loop()
        sink: asyncio.Future[bytes] = loop.create_future()
        self._pending = _Pending(head, frozenset(), sink)
        try:
            await asyncio.wait_for(sink, timeout)
            _LOGGER.debug("%s: discarded late ACK", self.address)
        except TimeoutError:
            pass
        finally:
            self._pending = None

    # ----------------------------------------------------------- ble notify
    @callback
    def _on_notify(self, _char, data: bytearray) -> None:
        payload = bytes(data)
        if not payload:
            return
        head = payload[0]
        if head in _PRIVATE_PUSHES:
            _LOGGER.debug("%s: rx %02x (text)", self.address, head)
        elif head != p.PUSH_STATUS:
            # Status frames arrive every second; keep them out of the log.
            _LOGGER.debug("%s: rx %s", self.address, payload.hex())

        if head == p.CMD_LOGIN and len(payload) > 1:
            if (
                self._login_future is not None
                and not self._login_future.done()
            ):
                self._login_future.set_result(payload)
            return

        if head & 0x80:
            self._handle_ack(payload)
            return

        self._handle_push(payload)

    @callback
    def _handle_ack(self, payload: bytes) -> None:
        pending = self._pending
        command = payload[0] & 0x7F
        if pending is not None and command == pending.head:
            if not pending.future.done():
                pending.future.set_result(payload)
            return
        # Unsolicited ACKs, legacy echoes included, carry no trustworthy
        # state without a request to pair them with.
        _LOGGER.debug("%s: unmatched ACK %s", self.address, payload.hex())

    @callback
    def _handle_push(self, payload: bytes) -> None:
        head = payload[0]
        reply = p.build_push_ack(payload)
        login = self.snapshot.login
        if reply is not None and login is not None and login.reply_required:
            self._write_soon(reply)

        previous_model = self.snapshot.model
        previous_status = self.snapshot.status
        handled = p.apply_push(self.snapshot, payload)
        if handled is None:
            if head != p.PUSH_STATUS:
                _LOGGER.debug(
                    "%s: ignored push %s", self.address, payload.hex()
                )
            return

        if handled == p.PUSH_CONTROL and self.snapshot.control is not None:
            control = self.snapshot.control
            self._reports_power = True
            self._power = control.power
            self._power_source = SOURCE_DEVICE
            self._muted = control.muted
            self._muted_source = SOURCE_DEVICE

        # ANP finishes its dump with the mode frame, others with control.
        if handled == p.PUSH_MODE or (
            handled == p.PUSH_CONTROL and self.model != "ANP"
        ):
            self._sync_done.set()

        pending = self._pending
        if (
            pending is not None
            and handled in pending.confirm_pushes
            and not pending.future.done()
            and (pending.matches is None or pending.matches(payload))
        ):
            pending.future.set_result(payload)

        if self.snapshot.model != previous_model and self.snapshot.model:
            for model_callback in list(self._model_listeners):
                model_callback(self.snapshot.model)

        if handled == p.PUSH_STATUS:
            # Status arrives about once a second with a countdown. Only
            # phase changes are worth a state write.
            status = self.snapshot.status
            if (
                previous_status is not None
                and status is not None
                and previous_status.phase == status.phase
                and previous_status.slot == status.slot
            ):
                return
        self._notify_listeners()

    @callback
    def _write_soon(self, frame: bytes) -> None:
        task = self.hass.async_create_background_task(
            self._async_write_quiet(frame),
            name=f"airversa_diffuser[{self.address}] ack",
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _async_write_quiet(self, frame: bytes) -> None:
        client = self._client
        if client is None or not client.is_connected:
            return
        try:
            await self._async_write(client, frame)
        except BleakError as err:
            _LOGGER.debug("%s: reply write failed: %s", self.address, err)

    @callback
    def _on_disconnect(self, _client) -> None:
        _LOGGER.debug("%s: link dropped", self.address)
        self._connected = False
        self._fail_pending("disconnected")
        login = self._login_future
        if login is not None and not login.done():
            login.set_exception(DiffuserError("disconnected during login"))
            login.exception()
        self._notify_listeners()

    # ------------------------------------------------------------- main loop
    async def _run(self) -> None:
        """Connect, hold, keepalive, and reconnect for the entry's lifetime."""
        while not self._stop.is_set():
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if device is None:
                _LOGGER.debug(
                    "%s: not currently advertising; waiting for it to appear",
                    self.address,
                )
                await self._async_wait_for_device()
                continue

            delay = RECONNECT_DELAY
            try:
                await self._async_connect_and_maintain(device)
            except asyncio.CancelledError:
                raise
            except PinRejectedError as err:
                self.last_error = str(err)
                _LOGGER.warning("%s: %s", self.address, err)
                delay = PIN_REJECTED_DELAY
            except DiffuserError as err:
                self.last_error = str(err)
                _LOGGER.debug("%s: %s", self.address, err)
            except Exception as err:  # noqa: BLE001 - keep the loop alive
                self.last_error = str(err)
                _LOGGER.debug("%s: connection error: %s", self.address, err)
            finally:
                await self._async_disconnect()

            if not self._stop.is_set():
                await asyncio.sleep(delay)

    async def _async_wait_for_device(self) -> None:
        """Block until the device is seen by any adapter/proxy, or timeout."""
        found = asyncio.Event()

        @callback
        def _on_advert(
            _service_info: bluetooth.BluetoothServiceInfoBleak,
            _change: bluetooth.BluetoothChange,
        ) -> None:
            found.set()

        cancel = bluetooth.async_register_callback(
            self.hass,
            _on_advert,
            bluetooth.BluetoothCallbackMatcher(
                address=self.address, connectable=True
            ),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        try:
            await asyncio.wait_for(found.wait(), timeout=DEVICE_WAIT_TIMEOUT)
        except TimeoutError:
            pass
        finally:
            cancel()

    async def _async_login(self, client: BleakClientWithServiceCache) -> None:
        """Send the PIN and decode the reply, if the firmware sends one."""
        loop = asyncio.get_running_loop()
        try:
            frame = p.build_login(self.pin)
        except p.ProtocolError as err:
            raise PinRejectedError(f"invalid PIN setting: {err}") from err
        self._login_future = loop.create_future()
        try:
            await self._async_write(client, frame)
            raw = await asyncio.wait_for(self._login_future, LOGIN_TIMEOUT)
        except TimeoutError:
            _LOGGER.debug("%s: no login reply; continuing", self.address)
            return
        finally:
            self._login_future = None

        login = p.parse_login_reply(raw)
        if not login.ok:
            raise PinRejectedError("device rejected the PIN")
        self.snapshot.login = login
        if login.firmware:
            self.firmware = login.firmware
        _LOGGER.debug(
            "%s: login ok (protocol %s, reply=%s)",
            self.address,
            login.protocol_version,
            login.reply_required,
        )

    async def _async_sync(self, client: BleakClientWithServiceCache) -> None:
        """Push the clock so the device sends its full state."""
        login = self.snapshot.login
        if not self.sync_state or login is None:
            return
        if (login.protocol_version or 0) < 3.0:
            return
        self._sync_done.clear()
        await self._async_write(client, p.build_time_sync(dt_util.now()))
        try:
            await asyncio.wait_for(self._sync_done.wait(), SYNC_TIMEOUT)
        except TimeoutError:
            _LOGGER.debug(
                "%s: state sync incomplete after timeout", self.address
            )

    async def _async_connect_and_maintain(self, device: BLEDevice) -> None:
        """Open the connection, log in, sync, then loop sending keepalives."""
        _LOGGER.debug("%s: connecting", self.address)
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            self.address,
            disconnected_callback=self._on_disconnect,
        )
        self._client = client
        self._mark_previous_session()
        try:
            await client.start_notify(p.CHAR_UUID, self._on_notify)
            await asyncio.sleep(HANDSHAKE_SETTLE)
            await self._async_login(client)
            await self._async_sync(client)
            await asyncio.sleep(KEEPALIVE_SETTLE)
            await self._async_write(client, p.KEEPALIVE)

            self._connected = True
            self.last_error = None
            self._notify_listeners()
            _LOGGER.info("%s: connected + handshaked", self.address)

            await self._async_apply_after_connect()

            while client.is_connected and not self._stop.is_set():
                await asyncio.sleep(self.keepalive)
                try:
                    await self._async_write(client, p.KEEPALIVE)
                except BleakError as err:
                    _LOGGER.debug(
                        "%s: keepalive failed (%s); reconnecting",
                        self.address,
                        err,
                    )
                    break
        finally:
            try:
                await client.stop_notify(p.CHAR_UUID)
            except Exception as err:  # noqa: BLE001 - best-effort cleanup
                _LOGGER.debug("%s: stop_notify failed: %s", self.address, err)

    def _mark_previous_session(self) -> None:
        """Demote last session's device state until the device re-reports it."""
        self.snapshot.login = None
        self.snapshot.control = None
        self.snapshot.status = None
        self.snapshot.mode = None
        self.snapshot.schedules.clear()
        if self._power_source is not None:
            self._power_source = SOURCE_PREVIOUS
        if self._muted_source is not None:
            self._muted_source = SOURCE_PREVIOUS

    async def _async_apply_after_connect(self) -> None:
        """Send queued work. Never overwrite state the device just reported."""
        target: bool | None = None
        queued_fresh = time.monotonic() - self._queued_at < QUEUE_TTL
        if self._queued_power is not None and queued_fresh:
            target = self._queued_power
        elif (
            self._desired is not None
            and self.snapshot.control is None
            and not self._reports_power
        ):
            # Device does not report state: keep the old re-apply behaviour.
            target = self._desired
        self._queued_power = None
        if target is None:
            return
        await asyncio.sleep(HANDSHAKE_SETTLE)
        try:
            async with self._power_lock:
                await self._async_send_power(target, self._mute_for_write())
        except DiffuserError as err:
            _LOGGER.debug("%s: re-apply failed: %s", self.address, err)
