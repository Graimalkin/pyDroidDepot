import asyncio
import pytest
from droiddepot.script import DroidScripts, DroidScriptProgrammer
from droiddepot.motor import DroidMotorDirection


async def _no_sleep(*_):
    return None


async def test_connect_logs_in_on_command_characteristic(make_connection):
    conn = make_connection()
    await conn.connect(silent=True)
    assert conn.droid.writes == ["222001", "222001"]
    await conn.disconnect(silent=True)


async def test_connect_plays_pairing_script_unless_silent(make_connection, monkeypatch):
    monkeypatch.setattr("droiddepot.connection.asyncio.sleep", _no_sleep)
    conn = make_connection()
    await conn.connect()
    # script 0x11, action execute(2): the same wire bytes upstream sent
    assert conn.droid.writes[2] == "25000c421102"
    await conn.disconnect(silent=True)


async def test_play_specific_sound_selects_bank_then_track(connected):
    await connected.audio_controller.play_audio(sound_id=1, bank_id=8)
    # Droid-Toolbox's activation sound sequence, byte for byte
    assert connected.droid.writes == ["27420f4444001f07", "27420f4444001800"]


async def test_play_random_from_bank(connected):
    await connected.audio_controller.play_audio(bank_id=3)
    assert connected.droid.writes == ["27420f4444001002"]


async def test_cycle_sends_no_parameter(connected):
    # Droid-Toolbox "play next track": 26 42 0f 43 44 00 1c
    await connected.audio_controller.play_audio(cycle=True)
    assert connected.droid.writes == ["26420f4344001c"]


async def test_bank_one_is_selected(connected):
    # upstream skipped selecting bank 1 because its 0-based id was falsy
    await connected.audio_controller.play_audio(sound_id=2, bank_id=1)
    assert connected.droid.writes == ["27420f4444001f00", "27420f4444001801"]


@pytest.mark.parametrize("volume,expected", [(20, "14"), (31, "1f"), (100, "1f"), (-5, "00")])
async def test_volume_is_clamped(connected, volume, expected):
    await connected.audio_controller.set_volume(volume)
    assert connected.droid.writes == ["27420f4444000e" + expected]


async def test_led_ids_are_hex(connected):
    await connected.audio_controller.disable_head_led(31)
    await connected.audio_controller.enable_head_led(16)
    assert connected.droid.writes == ["27420f4444004a1f", "27420f4444004b10"]


async def test_script_ids_are_hex(connected):
    await connected.script_engine.execute_script(DroidScripts.FirstOrderParkResponseScript)
    await connected.script_engine.execute_script(10)
    assert connected.droid.writes == ["25000c420702", "25000c420a02"]


async def test_dangerous_script_refused(connected):
    with pytest.raises(ValueError):
        await connected.script_engine.execute_script(DroidScripts.FullThrottleTestScript)
    assert connected.droid.writes == []


async def test_script_programmer_opens_and_closes(connected):
    async with DroidScriptProgrammer(connected, 20):
        pass
    assert connected.droid.writes == ["25000c421400", "25000c421401"]


async def test_center_head_and_drive(connected):
    await connected.motor_controller.center_head()
    await connected.motor_controller.set_drive_speed(0, 160, 300)
    assert connected.droid.writes == [
        "27420f444401ff00",
        "29000546" + "00" + "a0" + "012c" + "0000",
        "29000546" + "01" + "a0" + "012c" + "0000",
    ]


async def test_rotation_turns_toward_the_named_side(connected):
    # Motor select byte is direction nibble (0 forward, 8 backward) + motor (0 left, 1 right).
    # Turning right = left wheel forward, right wheel backward (verified on a real R2).
    await connected.motor_controller.set_rotation_speed(DroidMotorDirection.Right, 110, 300)
    await connected.motor_controller.set_rotation_speed(DroidMotorDirection.Left, 110, 300)
    assert connected.droid.writes == [
        "29000546" + "00" + "6e" + "012c" + "0000",
        "29000546" + "81" + "6e" + "012c" + "0000",
        "29000546" + "80" + "6e" + "012c" + "0000",
        "29000546" + "01" + "6e" + "012c" + "0000",
    ]


async def test_disconnect_stops_motors_and_cancels_heartbeat(make_connection):
    conn = make_connection()
    await conn.connect(silent=True)
    heartbeat = conn._heartbeat_task
    await conn.disconnect(silent=True)
    await asyncio.sleep(0)
    assert heartbeat.done()
    assert not conn.is_connected
    # drive motors (0 = left, 1 = right) at speed 0, default ramp 300, delay 0
    assert conn.clients[0].writes[-2:] == ["290005460000012c0000", "290005460100012c0000"]


async def test_heartbeat_runs_on_the_event_loop(make_connection, monkeypatch):
    monkeypatch.setattr("droiddepot.connection.HEARTBEAT_INTERVAL", 0.01)
    conn = make_connection()
    await conn.connect(silent=True)
    await asyncio.sleep(0.05)
    assert "23000e40" in conn.droid.writes
    await conn.disconnect(silent=True)


async def test_dropped_link_cancels_heartbeat_and_notifies(make_connection):
    dropped = []
    conn = make_connection(disconnected_callback=dropped.append)
    await conn.connect(silent=True)
    heartbeat = conn._heartbeat_task
    conn.droid.drop()
    await asyncio.sleep(0)
    assert heartbeat.done()
    assert dropped == [conn]


async def test_concurrent_writes_are_serialized(connected):
    order = []
    original = connected.droid.write_gatt_char

    async def slow_write(char, data, response=None):
        order.append("start")
        await asyncio.sleep(0.01)
        order.append("end")
        await original(char, data, response)

    connected.droid.write_gatt_char = slow_write
    await asyncio.gather(connected.audio_controller.set_volume(1), connected.audio_controller.set_volume(2))
    assert order == ["start", "end", "start", "end"]


async def test_firmware_response_resolves(connected):
    task = asyncio.create_task(connected.get_droid_firmware_information())
    await asyncio.sleep(0)
    payload = bytes.fromhex("4b1001444411110100000000")
    # size byte is the packet length + 0x1f
    packet = bytes([len(payload) + 4 + 0x1f, 0x00, 0x81, 0x40 + len(payload)]) + payload
    await connected.droid.notify_handler(None, bytearray(packet))
    assert await task == payload.hex()
