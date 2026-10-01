import pytest
from droiddepot.connection import DroidConnection
from droiddepot.protocol import DroidBluetoothCharacteristics


class FakeClient:
    """Stands in for BleakClient and records every write."""

    def __init__(self, profile, disconnected_callback=None, timeout=None):
        self.profile = profile
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.writes = []
        self.notify_handler = None

    async def connect(self):
        self.is_connected = True

    async def start_notify(self, char, handler):
        assert char == DroidBluetoothCharacteristics.DroidNotifyCharacteristic
        self.notify_handler = handler

    async def write_gatt_char(self, char, data, response=None):
        assert char == DroidBluetoothCharacteristics.DroidCommandCharacteristic
        assert response is False
        self.writes.append(bytes(data).hex())

    async def disconnect(self):
        if self.is_connected:
            self.is_connected = False
            if self.disconnected_callback:
                self.disconnected_callback(self)

    def drop(self):
        """Simulate the droid vanishing (powered off / out of range)."""
        self.is_connected = False
        self.disconnected_callback(self)


@pytest.fixture
def make_connection():
    clients = []

    def factory(*args, **kwargs):
        client = FakeClient(*args, **kwargs)
        clients.append(client)
        return client

    def make(address="AA:BB:CC:DD:EE:FF", manufacturer_data=None, **kwargs):
        conn = DroidConnection(address, manufacturer_data, client_factory=factory, **kwargs)
        conn.clients = clients
        return conn

    return make


@pytest.fixture
async def connected(make_connection):
    conn = make_connection()
    await conn.connect(silent=True)
    conn.droid.writes.clear()
    yield conn
    await conn.disconnect(silent=True)
