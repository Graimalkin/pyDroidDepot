"""
Hardware smoke test. Run this first on a new host to confirm BLE, discovery and the
protocol all work before building anything on top.

    python examples/smoketest.py scan
    python examples/smoketest.py test AA:BB:CC:DD:EE:FF
    python examples/smoketest.py test AA:BB:CC:DD:EE:FF --pairing   # also try the pairing chirp
    python examples/smoketest.py session AA:BB:CC:DD:EE:FF          # exercise connect-on-demand

The droid must be powered on and NOT connected to the Disney app or a phone,
otherwise it stops advertising and can't be found.
"""

import argparse
import asyncio
import logging
from droiddepot import discover_droids, find_droid, DroidSession
from droiddepot.motor import DroidMotorDirection


def step(message: str) -> None:
    print("\n>>> %s" % message, flush=True)


async def scan(args) -> None:
    step("Scanning for %ss..." % args.timeout)
    droids = await discover_droids(timeout=args.timeout)
    if not droids:
        print("No droids found. Are they on, in range, and disconnected from the app?")
        return
    for droid in droids:
        print("  %s  personality=%s  affiliation=%s" % (droid.address, droid.personality_id, droid.affiliation_id))


async def test(args) -> None:
    step("Finding %s" % args.address)
    droid = await find_droid(args.address, timeout=args.timeout)
    if droid is None:
        print("Not found.")
        return
    print("  personality=%s affiliation=%s" % (droid.personality_id, droid.affiliation_id))

    step("Connecting (pairing chirp: %s)" % ("yes" if args.pairing else "no"))
    await droid.connect(silent=not args.pairing)

    try:
        step("Firmware info")
        try:
            print("  %s" % await droid.get_droid_firmware_information())
        except TimeoutError:
            print("  (no response; not fatal)")

        step("Volume 15, then a random sound from bank 1")
        await droid.audio_controller.set_volume(15)
        await droid.audio_controller.play_audio(bank_id=1)
        await asyncio.sleep(3)

        step("Specific sound: bank 2, sound 1")
        await droid.audio_controller.play_audio(sound_id=1, bank_id=2)
        await asyncio.sleep(3)

        step("Next sound in the selected bank (cycle)")
        await droid.audio_controller.play_audio(cycle=True)
        await asyncio.sleep(3)

        step("Head: turn one way, then center")
        await droid.motor_controller.set_head_speed(DroidMotorDirection.Forward, 120)
        await asyncio.sleep(1)
        await droid.motor_controller.set_head_speed(DroidMotorDirection.Forward, 0)
        await droid.motor_controller.center_head()
        await asyncio.sleep(2)

        step("Built-in reaction script 1")
        await droid.script_engine.execute_script(1)
        await asyncio.sleep(5)

        step("Holding the connection for 25s to prove the heartbeat keeps it alive")
        await asyncio.sleep(25)
        print("  still connected: %s" % droid.is_connected)
    finally:
        step("Disconnecting (with shutdown sound)")
        await droid.disconnect()


async def session(args) -> None:
    session = DroidSession(args.address, idle_timeout=10, on_state_change=lambda up: print("  [link %s]" % ("UP" if up else "DOWN")))

    step("First command connects on demand")
    await session.run(lambda d: d.audio_controller.play_audio(bank_id=1))
    await asyncio.sleep(3)

    step("Second command reuses the link")
    await session.run(lambda d: d.motor_controller.center_head())

    step("Waiting 15s; the 10s idle timeout should drop the link")
    await asyncio.sleep(15)

    step("Next command reconnects")
    await session.run(lambda d: d.audio_controller.play_audio(bank_id=1))
    await asyncio.sleep(3)
    await session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true", help="log every BLE write")
    parser.add_argument("--timeout", type=float, default=15.0, help="scan timeout in seconds")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan")
    t = sub.add_parser("test")
    t.add_argument("address")
    t.add_argument("--pairing", action="store_true", help="play the pairing animation on connect")
    s = sub.add_parser("session")
    s.add_argument("address")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run({"scan": scan, "test": test, "session": session}[args.command](args))


if __name__ == "__main__":
    main()
