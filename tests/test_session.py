import asyncio
import pytest
from droiddepot.session import DroidSession, DroidNotFoundError


def make_session(make_connection, available=True, **kwargs):
    calls = {"find": 0, "states": []}

    async def finder(address, timeout, disconnected_callback):
        calls["find"] += 1
        if not available:
            return None
        conn = make_connection(address, disconnected_callback=disconnected_callback)
        calls["last"] = conn
        return conn

    kwargs.setdefault("idle_timeout", 0.05)
    session = DroidSession("AA:BB:CC:DD:EE:FF", finder=finder, on_state_change=calls["states"].append, **kwargs)
    return session, calls


async def test_connects_lazily_and_runs_command(make_connection):
    session, calls = make_session(make_connection)
    assert calls["find"] == 0
    await session.run(lambda d: d.audio_controller.set_volume(10))
    assert session.is_connected
    assert calls["last"].droid.writes[-1] == "27420f4444000e0a"
    assert calls["states"] == [True]
    await session.close()


async def test_reuses_connection_between_commands(make_connection):
    session, calls = make_session(make_connection, idle_timeout=10)
    await session.run(lambda d: d.motor_controller.center_head())
    await session.run(lambda d: d.motor_controller.center_head())
    assert calls["find"] == 1
    await session.close()


async def test_idle_timeout_disconnects(make_connection):
    session, calls = make_session(make_connection)
    await session.run(lambda d: d.motor_controller.center_head())
    await asyncio.sleep(0.15)
    assert not session.is_connected
    assert calls["states"] == [True, False]


async def test_commands_reset_idle_timer(make_connection):
    session, calls = make_session(make_connection, idle_timeout=0.1)
    for _ in range(4):
        await session.run(lambda d: d.motor_controller.center_head())
        await asyncio.sleep(0.05)
    assert session.is_connected
    assert calls["find"] == 1
    await session.close()


async def test_reconnects_after_idle_disconnect(make_connection):
    session, calls = make_session(make_connection)
    await session.run(lambda d: d.motor_controller.center_head())
    await asyncio.sleep(0.15)
    await session.run(lambda d: d.motor_controller.center_head())
    assert calls["find"] == 2
    assert calls["states"] == [True, False, True]
    await session.close()


async def test_reconnects_after_link_drop(make_connection):
    session, calls = make_session(make_connection, idle_timeout=10)
    await session.run(lambda d: d.motor_controller.center_head())
    calls["last"].droid.drop()
    assert calls["states"] == [True, False]
    await session.run(lambda d: d.motor_controller.center_head())
    assert calls["find"] == 2
    await session.close()


async def test_missing_droid_raises_after_retries(make_connection, monkeypatch):
    async def no_sleep(*_):
        return None
    monkeypatch.setattr("droiddepot.session.asyncio.sleep", no_sleep)
    session, calls = make_session(make_connection, available=False, connect_attempts=3)
    with pytest.raises(DroidNotFoundError):
        await session.run(lambda d: d.motor_controller.center_head())
    assert calls["find"] == 3
    assert calls["states"] == []


async def test_concurrent_commands_queue(make_connection):
    session, calls = make_session(make_connection, idle_timeout=10)
    await asyncio.gather(*(session.run(lambda d: d.motor_controller.center_head()) for _ in range(5)))
    assert calls["find"] == 1
    assert calls["last"].droid.writes.count("27420f444401ff00") == 5
    await session.close()


async def test_close_is_idempotent(make_connection):
    session, calls = make_session(make_connection)
    await session.close()
    await session.run(lambda d: d.motor_controller.center_head())
    await session.close()
    await session.close()
    assert calls["states"] == [True, False]
