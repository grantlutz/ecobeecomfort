"""In-process hub between thermostat devices and their room-sensor devices.

The whole app runs in one Python process, so a module-level registry is the
simplest reliable channel: each thermostat device publishes its poll here, and
the room-sensor devices for that thermostat are fed from it. Sensors therefore
cost zero extra API calls — remoteSensors arrive in the poll ecobee's rate
limits already budget for — and pairing a sensor needs no sign-in, because the
data is already on hand.

The trade-off: a room sensor only works while its thermostat is paired, and its
readings tick at the thermostat's poll cadence.
"""

from __future__ import annotations

from typing import Any

# identifier -> the latest full thermostat payload from that device's poll.
_latest: dict[str, dict] = {}

# identifier -> {sensor_id -> sensor device} awaiting updates.
_sensors: dict[str, dict[str, Any]] = {}


def set_latest(identifier: str, thermostat: dict) -> None:
    _latest[str(identifier)] = thermostat


def get_latest() -> dict[str, dict]:
    return dict(_latest)


def register_sensor(identifier: str, sensor_id: str, device: Any) -> None:
    _sensors.setdefault(str(identifier), {})[str(sensor_id)] = device


def unregister_sensor(identifier: str, sensor_id: str) -> None:
    _sensors.get(str(identifier), {}).pop(str(sensor_id), None)


def sensors_for(identifier: str) -> list[Any]:
    return list(_sensors.get(str(identifier), {}).values())
