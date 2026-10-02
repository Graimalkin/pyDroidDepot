<img src=".github/logo.png" align="right" width="250">

# PyDroidDepot

PyDroidDepot is an open-source project that aims to provide Python access to the Starwars Galaxy's Edge droid depot droids. It allows users to manually control their custom built droids using custom applications.

# Supported Droid Types

* R Unit
* C Unit
* B Unit (WIP)
* BD Unit (WIP)

Droid types that are marked as work in progress will function though they may have unexpected behaviour or may be missing functionality.

## About this fork

This fork modernizes upstream for long-running services (e.g. a Home Assistant bridge):

* **Runs on one event loop.** Upstream ran the heartbeat on a second event loop in a separate thread, sharing the same BLE client, and used blocking `time.sleep()` inside async code. Both are gone; writes are serialized with a lock and time limited.
* **Current bleak.** `bleak>=1.0,<4` instead of a pin to 0.20.2. Python 3.10+.
* **Several droids at once.** `find_droid(mac)` connects to a specific droid; `discover_droids()` returns all of them and respects `retry=False`.
* **Connect on demand.** `DroidSession` connects lazily on the first command, disconnects after an idle timeout, reconnects after drops, and queues overlapping commands.
* **Protocol fixes**, checked byte for byte against [Droid-Toolbox](https://github.com/ruthsarian/Droid-Toolbox):
  * `play_audio` always took the "specific track" path, so `cycle=True` and "random sound from bank" never ran, and bank 1 was never selected.
  * Volume is clamped to the droid's real range of 0-31.
  * LED and script ids are hex encoded. Upstream sent decimal digits; the pairing script constants keep their old wire values (0x11/0x12).
  * Login is written to the command characteristic UUID instead of a hardcoded GATT handle.
  * A firmware mismatch now logs a warning instead of raising inside bleak's notification callback.
  * Fixed `DroidScriptProgrammer` and `get_available_audio_in_bank`.
* **Optional beacons.** `pydBeacon` is only needed for park beacon reactions: `pip install ".[beacon]"`.
* **Tests.** Run `pip install -e ".[test]" && pytest`.

```python
from droiddepot import DroidSession

session = DroidSession("AA:BB:CC:DD:EE:FF", idle_timeout=120)
await session.run(lambda d: d.audio_controller.play_audio(bank_id=2))   # connects, beeps
await session.run(lambda d: d.motor_controller.center_head())           # reuses the link
# ...2 minutes of quiet later the session disconnects on its own
```

## Installation

```
pip install "git+https://github.com/Graimalkin/pyDroidDepot"
```

Or from a checkout:

```
pip install -e ".[test]"
```

## Examples

Examples are under `examples/`. Start with `examples/smoketest.py` on any new host. It scans, connects, plays sounds, moves the head and exercises the on-demand session:

```
python examples/smoketest.py scan
python examples/smoketest.py test AA:BB:CC:DD:EE:FF
python examples/smoketest.py session AA:BB:CC:DD:EE:FF
```

A droid only advertises while it is **not** connected to the Disney app or a phone.

## License

PyDroidDepot is released under the MIT license. See the LICENSE file for more details.
