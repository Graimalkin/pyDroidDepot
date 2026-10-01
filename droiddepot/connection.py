"""
Copyright (c) Jordan Maxwell, All Rights Reserved.
See LICENSE file in the project root for full license information.

DroidConnection is a BLE class representing a connection to a SWGE DroidDepot droid.
It includes methods for connecting, disconnecting, sending commands, and running scripts on the droid.

It also includes instances of DroidAudioController, DroidMotorController, and DroidScriptEngine
to manage the droid's audio, motor, and script functions.

Everything in this module runs on a single asyncio event loop. Writes to the droid are
serialized with a lock so concurrent callers (heartbeat, automations, scripts) can never
interleave GATT writes.
"""

import asyncio
from contextlib import AbstractAsyncContextManager
import logging
from typing import Callable, Optional, Union
from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from droiddepot.protocol import *
from droiddepot.audio import DroidAudioController
from droiddepot.motor import DroidMotorController
from droiddepot.script import DroidScriptEngine, DroidScripts
from droiddepot.voice import DroidVoiceController
from droiddepot.notify import DroidNotificationProcessor
from droiddepot.hardware import DroidPersonalityIdentifier, DroidAffiliation

logger = logging.getLogger(__name__)

# The droid drops idle connections, so we poke it with a harmless command on this interval.
HEARTBEAT_INTERVAL = 10.0

# Upper bound for a single GATT write. BlueZ can wedge a write forever when the link degrades.
WRITE_TIMEOUT = 5.0

def build_droid_command(command_id: int, data: str = "") -> bytearray:
    """
    Creates the bytearray for a droid command.

    The first byte is the total length of the command (header + data) or'ed with 0x20. The second byte is 0x42
    for multipurpose commands (id 15), or 0x00 otherwise. The third byte is the command id itself. The fourth
    byte is the length of the data plus 0x40. The remaining bytes are the data itself.

    Args:
        command_id (int): The command id to be included in the Droid command
        data (str): The data to be included in the Droid command as a hex string

    Returns:
        bytearray: The packed command

    Raises:
        ValueError: If the data string is not valid hex
    """

    try:
        payload = bytes.fromhex(data)
    except ValueError:
        raise ValueError("Failed to pack droid command (%s) with data (%s). Data is malformed" % (command_id, data))

    header_length = 3
    byte1 = (len(payload) + header_length) | 0x20
    byte2 = 0x42 if command_id == DroidCommandId.MultipurposeCommand else 0x00
    byte3 = command_id
    byte4 = len(payload) + 0x40

    command_bytes = bytearray([byte1, byte2, byte3, byte4])
    command_bytes.extend(payload)
    return command_bytes

def build_droid_multi_command(command_id: int, data: str = "") -> bytearray:
    """
    Creates the bytearray for a multipurpose droid command (command 15 with a 0x44 sub-header).

    Args:
        command_id (int): The multipurpose command id (see DroidMultipurposeCommand)
        data (str): The data to be included in the command as a hex string
    """

    return build_droid_command(DroidCommandId.MultipurposeCommand, "44%02x%s" % (command_id, data))

def parse_manufacturer_data(manufacturer_data: Optional[dict]) -> tuple:
    """
    Extracts the (personality_id, affiliation_id) from a droid's advertisement manufacturer data.
    Falls back to an R unit scoundrel when the data is missing.
    """

    if not manufacturer_data or DisneyBLEManufacturerId.DroidManufacturerId not in manufacturer_data:
        return (DroidPersonalityIdentifier.RUnit, DroidAffiliation.Scoundrel)

    droid_data = manufacturer_data[DisneyBLEManufacturerId.DroidManufacturerId]
    if len(droid_data) < 2:
        return (DroidPersonalityIdentifier.RUnit, DroidAffiliation.Scoundrel)

    personality_id = droid_data[-1]
    affiliation_id = (droid_data[-2] - 0x80) // 2
    return (personality_id, affiliation_id)

class DroidConnection(AbstractAsyncContextManager):
    """
    Represents a connection to a SWGE DroidDepot droid.

    Args:
        profile (BLEDevice | str): The BLE device (preferred) or MAC address of the droid.
        manufacturer_data (dict): The manufacturer data from the droid's advertisement, if known.
        disconnected_callback (callable): Optional callback invoked with this connection when the link drops.
        client_factory (callable): Builds the BLE client. Overridable for tests.
    """

    DroidServiceId = '09b600a0-3e42-41fc-b474-e9c0c8f0c801'

    def __init__(self, profile: Union[BLEDevice, str], manufacturer_data: Optional[dict] = None,
                 disconnected_callback: Optional[Callable[["DroidConnection"], None]] = None,
                 client_factory: Callable[..., BleakClient] = BleakClient):
        """
        Initializes a new instance of the DroidConnection class.
        """

        self.profile = profile
        self.droid = None
        self.manufacturer_data = manufacturer_data
        self.personality_id, self.affiliation_id = parse_manufacturer_data(manufacturer_data)
        self.disconnected_callback = disconnected_callback
        self.client_factory = client_factory

        self.audio_controller = DroidAudioController(self)
        self.script_engine = DroidScriptEngine(self)
        self.motor_controller = DroidMotorController(self)
        self.voice_controller = DroidVoiceController(self)
        self.notify_processor = DroidNotificationProcessor(self)

        self._write_lock = asyncio.Lock()
        self._heartbeat_task: Optional[asyncio.Task] = None

    @property
    def address(self) -> str:
        """
        The MAC address (or platform identifier) of the droid.
        """

        return self.profile.address if isinstance(self.profile, BLEDevice) else str(self.profile)

    @property
    def is_connected(self) -> bool:
        """
        True when the BLE link to the droid is up.
        """

        return self.droid is not None and self.droid.is_connected

    async def connect(self, silent: bool = False, timeout: float = 20.0) -> None:
        """
        Connect to the Droid using BLE.

        Args:
            silent (bool): Skip the pairing animation/chirp the droid plays on connect.
            timeout (float): Seconds to wait for the BLE connection to establish.
        """

        self.droid = self.client_factory(self.profile, disconnected_callback=self._on_disconnected, timeout=timeout)
        await self.droid.connect()
        await self.droid.start_notify(DroidBluetoothCharacteristics.DroidNotifyCharacteristic, self.notification_handler)

        # The droid ignores commands until it sees this "login" value. Sending it twice matches
        # the official app; it also turns off the droid's own beacon until we disconnect.
        connect_code = bytearray.fromhex("222001")
        for _ in range(2):
            await self.write(connect_code)
            await asyncio.sleep(0.1)

        if not silent:
            await self.script_engine.execute_script(DroidScripts.DroidPairingSequence1)
            await asyncio.sleep(4)

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def __aenter__(self) -> object:
        """
        Connect to the droid when the connection is opened.
        """

        await self.connect()
        return self

    async def notification_handler(self, sender: object, data: bytearray) -> None:
        """
        Processes notification events from the connected droid and
        passes it to our notify message processor.
        """

        await self.notify_processor.handle_incoming_message(sender, data)

    def _on_disconnected(self, client: BleakClient) -> None:
        """
        Called by bleak when the link drops, whether we asked for it or not.
        """

        logger.info("Droid %s disconnected", self.address)
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self.disconnected_callback is not None:
            self.disconnected_callback(self)

    async def _heartbeat_loop(self) -> None:
        """
        Sends a harmless unused command periodically to keep the connection alive even when not in use.
        """

        try:
            while self.is_connected:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not self.is_connected:
                    break
                await self.send_droid_command(DroidCommandId.ConnectionHeartbeat)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # A failed heartbeat means the link is dying; bleak's disconnect callback handles the rest.
            logger.warning("Heartbeat to droid %s failed: %s", self.address, err)

    async def disconnect(self, silent: bool = False) -> None:
        """
        Disconnect from the Droid.

        Args:
            silent (bool): Skip the shutdown sound.
        """

        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if not self.is_connected:
            return

        logger.info("Disconnecting from droid %s", self.address)
        try:
            await self.motor_controller.set_head_speed(0, 0)
            await self.motor_controller.set_drive_speed(0, 0)
            if not silent:
                await self.audio_controller.play_shutdown_audio()
        except Exception as err:
            logger.warning("Shutdown commands to droid %s failed: %s", self.address, err)
        finally:
            await self.droid.disconnect()

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """
        Disconnects from the Droid when the connection is closed.
        """

        await self.disconnect()

    async def write(self, payload: bytearray) -> None:
        """
        Writes raw bytes to the droid's command characteristic. Writes are serialized and time limited.
        """

        if self.droid is None:
            raise ConnectionError("Droid %s is not connected" % self.address)

        async with self._write_lock:
            logger.debug('Sending command: %s', payload.hex())
            await asyncio.wait_for(
                self.droid.write_gatt_char(DroidBluetoothCharacteristics.DroidCommandCharacteristic, payload, response=False),
                timeout=WRITE_TIMEOUT)

    def build_droid_command(self, command_id: int, data: str) -> bytearray:
        """
        Kept for API compatibility. See the module level build_droid_command.
        """

        return build_droid_command(command_id, data)

    async def send_droid_command(self, command_id: int, data: str = "") -> None:
        """
        Sends a command to the Droid, composed of a command ID and optional data.

        If the data string is malformed, a ValueError is raised.

        Args:
            command_id (int): The ID of the command to send.
            data (str): Optional data to include in the command, as a string of hexadecimal digits.
        """

        await self.write(build_droid_command(command_id, data))

    async def send_droid_multi_command(self, command_id: int, data: str = "") -> None:
        """
        Sends a multi command to the Droid, composed of a command ID and optional data.

        If the data string is malformed, a ValueError is raised.

        Args:
            command_id (int): The ID of the command to send.
            data (str): Optional data to include in the command, as a string of hexadecimal digits.
        """

        await self.write(build_droid_multi_command(command_id, data))

    async def get_droid_firmware_information(self) -> str:
        """
        Requests the droid firmware information. Currently the contents of this data
        is unknown. Because of this this request only returns the raw data processed by the
        notify command processor.
        """

        response = self.notify_processor.expect_response(DroidCommandId.RetrieveFirmwareInformationResponse)
        await self.send_droid_command(DroidCommandId.RetrieveFirmwareInformation)
        firmware_information = await self.notify_processor.wait_for_response(response)
        if firmware_information is None:
            raise TimeoutError('Failed to retrieve firmware information. No response given')
        return firmware_information

    async def set_pairing_led(self, state: bool) -> None:
        """
        Sets the active state of the droid's pairing led.

        Args:
            state (bool): State to set the led to
        """

        data = "00"
        data += "ff" if state else "00"
        await self.send_droid_command(DroidCommandId.SetPairingLedState, data)

    async def set_rgb_led(self, state: bool) -> None:
        """
        Sets the active state of the droid's onboard RGB led. Currently no droids exist that use this feature
        however its added for completeness.

        Args:
            state (bool): State to set the led to
        """

        data = "00"
        data += "ff" if state else "00"
        await self.send_droid_command(DroidCommandId.SetRGBLedState, data)

    async def flash_pairing_led(self, data: str) -> None:
        """
        Flashes the droids onboard pairing led. Currently the data required for this command has not been decoded.

        An example piece of encoded data is "020001ff01ff0aff00". This hex encoded string will flash the pairing LED 10 times at
        a rate of once per second.
        """

        await self.send_droid_command(DroidCommandId.FlashPairingLed, data)

def is_droid_advertisement(ble_device: BLEDevice, advertising_data: object) -> bool:
    """
    True when an advertisement comes from a Droid Depot droid.
    """

    manufacturer_data = advertising_data.manufacturer_data or {}
    name = advertising_data.local_name or ble_device.name
    return name == "DROID" and DisneyBLEManufacturerId.DroidManufacturerId in manufacturer_data

async def discover_droids(retry: bool = False, timeout: float = 10.0) -> list:
    """
    Scans for nearby Bluetooth devices manufactured by Disney with the device name of "DROID" and returns
    a DroidConnection for each one.

    Args:
        retry (bool): Keep scanning until at least one droid is found or the task is cancelled.
        timeout (float): Seconds per scan window.

    Returns:
        a list of DroidConnection objects for the discovered droids. Empty when none were found and retry is False.
    """

    while True:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        droid_connections = [
            DroidConnection(ble_device, advertising_data.manufacturer_data)
            for ble_device, advertising_data in discovered.values()
            if is_droid_advertisement(ble_device, advertising_data)]

        for droid in droid_connections:
            logger.info("Droid discovered: %s (personality %s)", droid.address, droid.personality_id)

        if droid_connections or not retry:
            return droid_connections

        logger.warning("Droid discovery found nothing. Retrying...")

async def discover_droid(retry: bool = False, timeout: float = 10.0) -> Optional[DroidConnection]:
    """
    Scans for nearby droids and returns the first one found.

    Args:
        retry (bool): Keep scanning until a droid is found or the task is cancelled.
        timeout (float): Seconds per scan window.

    Returns:
        a DroidConnection for the first droid found, otherwise None
    """

    discovered_droids = await discover_droids(retry, timeout)
    return discovered_droids[0] if discovered_droids else None

async def find_droid(address: str, timeout: float = 15.0, **kwargs) -> Optional[DroidConnection]:
    """
    Scans for one specific droid by MAC address. Use this when more than one droid is in range.

    Args:
        address (str): The droid's MAC address (case insensitive).
        timeout (float): Seconds to scan before giving up.
        kwargs: Passed through to DroidConnection (e.g. disconnected_callback).

    Returns:
        a DroidConnection for the droid, or None if it was not seen advertising. A droid that is
        already connected to something else (e.g. the Disney app) does not advertise.
    """

    found = {}
    def match(ble_device: BLEDevice, advertising_data: object) -> bool:
        if ble_device.address.upper() != address.upper() or not is_droid_advertisement(ble_device, advertising_data):
            return False
        found['manufacturer_data'] = advertising_data.manufacturer_data
        return True

    ble_device = await BleakScanner.find_device_by_filter(match, timeout=timeout)
    if ble_device is None:
        return None
    return DroidConnection(ble_device, found.get('manufacturer_data'), **kwargs)
