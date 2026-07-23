"""ecobee room sensor driver — pairing straight from paired thermostats.

No sign-in here: the sensors are listed out of the latest polls the already-
paired thermostat devices published to the in-process registry. Pairing a
thermostat first is therefore a prerequisite, and the pair view says so when
the registry is empty.
"""

from __future__ import annotations

import os
import sys
from typing import Any

# Make the app-root `ecobee` package importable (named `ecobee`, not `lib`,
# which the Homey runner already owns — see the thermostat driver).
_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from homey.driver import Driver, ListDeviceProperties  # noqa: E402

from ecobee import registry  # noqa: E402
from ecobee.api import (  # noqa: E402
    identifier_of,
    remote_sensors_of,
    sensor_features_of,
    sensor_label_of,
)

FEATURE_CAPABILITIES = {
    "temperature": "measure_temperature",
    "occupancy": "alarm_motion",
    "humidity": "measure_humidity",
}


class EcobeeSensorDriver(Driver):
    async def on_init(self) -> None:
        self.log("ecobee room sensor driver initialized")

    async def on_pair(self, session) -> None:
        async def handle_list_devices(_data: Any = None) -> list[ListDeviceProperties]:
            thermostats = registry.get_latest()
            if not thermostats:
                raise Exception(
                    "Pair an ecobee Thermostat first — the room sensors are "
                    "read through it. If one is already paired, give it a few "
                    "minutes to complete a poll and try again."
                )
            out: list[ListDeviceProperties] = []
            for thermostat in thermostats.values():
                thermostat_id = identifier_of(thermostat)
                for sensor in remote_sensors_of(thermostat):
                    sensor_id = str(sensor.get("id") or "")
                    if not sensor_id:
                        continue
                    features = sensor_features_of(sensor)
                    if not features:
                        continue
                    out.append(
                        {
                            "name": sensor_label_of(sensor, thermostat),
                            "data": {"id": f"{thermostat_id}/{sensor_id}"},
                            "capabilities": [
                                FEATURE_CAPABILITIES[f]
                                for f in features
                                if f in FEATURE_CAPABILITIES
                            ],
                            "store": {
                                "thermostat_id": thermostat_id,
                                "sensor_id": sensor_id,
                                "features": features,
                            },
                        }
                    )
            self.log(f"[pair] list_devices -> {[o['name'] for o in out]}")
            return out

        session.set_handler("list_devices", handle_list_devices)


homey_export = EcobeeSensorDriver
