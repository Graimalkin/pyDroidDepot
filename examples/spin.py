"""
"""

from random import randrange
from droiddepot.connection import discover_droid, DroidCommandId
from droiddepot.motor import DroidMotorDirection, DroidMotorIdentifier
from bleak import BleakError
import asyncio

async def main() -> None:
    """
    Main entry point into the example application
    """

    droid = await discover_droid(retry=True)
    try:
        async with droid as d:
            current_direction = DroidMotorDirection.Forward
            while d.is_connected:
                await d.motor_controller.set_motor_speed(current_direction, DroidMotorIdentifier.LeftMotor, 100, 300)
                await asyncio.sleep(5)  
                if current_direction == DroidMotorDirection.Forward:
                    current_direction = DroidMotorDirection.Backwards
                else:
                    current_direction = DroidMotorDirection.Forward
            
    except OSError as err:
        print(f"Discovery failed due to operating system: {err}")
    except BleakError as err:
        print(f"Discovery failed due to Bleak: {err}")
    except KeyboardInterrupt as err:
        pass
    finally:
        print("Shutting down.")

# Main entry point into the example application
if __name__ == "__main__":
    asyncio.run(main())
