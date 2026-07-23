"""ecobee room sensor device — temperature, motion, and (built-in only) humidity.

A room sensor makes no API calls of its own: it registers with the in-process
registry and is fed each time its parent thermostat device polls. That keeps
ecobee's rate limits untouched however many sensors are added — the trade-off
being that readings tick at the thermostat's poll cadence (about five minutes),
so the motion capability suits presence logic, not burglar alarms.
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

from homey.device import Device  # noqa: E402

from ecobee import registry  # noqa: E402
from ecobee.api import (  # noqa: E402
    find_remote_sensor,
    sensor_humidity_of,
    sensor_occupancy_of,
    sensor_temperature_of,
)

FEATURE_CAPABILITIES = {
    "temperature": "measure_temperature",
    "occupancy": "alarm_motion",
    "humidity": "measure_humidity",
}


class EcobeeSensorDevice(Device):
    async def on_init(self) -> None:
        store = self.get_store()
        self.thermostat_id = str(store.get("thermostat_id") or "")
        self.sensor_id = str(store.get("sensor_id") or "")
        features = store.get("features") or ["temperature", "occupancy"]

        # Carry exactly the capabilities this sensor actually reports.
        wanted = {FEATURE_CAPABILITIES[f] for f in features if f in FEATURE_CAPABILITIES}
        for cap in wanted:
            if not self.has_capability(cap):
                try:
                    await self.add_capability(cap)
                except Exception as e:  # noqa: BLE001
                    self.error(f"add_capability {cap} failed:", e)
        for cap in FEATURE_CAPABILITIES.values():
            if cap not in wanted and self.has_capability(cap):
                try:
                    await self.remove_capability(cap)
                except Exception as e:  # noqa: BLE001
                    self.error(f"remove_capability {cap} failed:", e)

        registry.register_sensor(self.thermostat_id, self.sensor_id, self)

        latest = registry.get_latest().get(self.thermostat_id)
        if latest:
            await self.apply_thermostat(latest)
        else:
            # The thermostat device feeds us on its next poll; until then (or if
            # the thermostat was removed from Homey) there is nothing to show.
            await self.set_unavailable(
                "Waiting for the ecobee thermostat this sensor belongs to."
            )
        self.log("ecobee room sensor initialized:", self.sensor_id)

    async def on_uninit(self) -> None:
        registry.unregister_sensor(self.thermostat_id, self.sensor_id)

    async def on_deleted(self) -> None:
        await self.on_uninit()

    # ── Fed by the thermostat device after every poll ────────────────────────
    async def apply_thermostat(self, thermostat: dict) -> None:
        sensor = find_remote_sensor(thermostat, self.sensor_id)
        if not sensor:
            await self.set_unavailable(
                "This sensor is no longer registered to its ecobee thermostat."
            )
            return
        await self.set_available()

        temperature = sensor_temperature_of(sensor)
        if temperature is not None:
            await self._safe_set("measure_temperature", round(temperature, 1))
        occupancy = sensor_occupancy_of(sensor)
        if occupancy is not None:
            await self._safe_set("alarm_motion", occupancy)
        humidity = sensor_humidity_of(sensor)
        if humidity is not None:
            await self._safe_set("measure_humidity", round(humidity))

    # ── Helpers ──────────────────────────────────────────────────────────────
    async def _safe_set(self, cap: str, value: Any) -> None:
        if not self.has_capability(cap):
            return
        try:
            if self.get_capability_value(cap) != value:
                await self.set_capability_value(cap, value)
        except Exception as e:  # noqa: BLE001
            self.error(f"set {cap} failed:", e)


homey_export = EcobeeSensorDevice
