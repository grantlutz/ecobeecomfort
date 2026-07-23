"""Standalone self-test for the ecobee lib — no Homey required.

Two modes:

  OFFLINE (default): runs the thermostat-JSON readers against a realistic
  payload, covering the cases that decide what the comfort picker shows —
  no hold, a profile hold, ecobee's own auto-away event, a bare setpoint
  hold, and a vacation. No network.

  LIVE (set env vars): actually signs in to ecobee, lists the thermostats on
  the account and prints each one's comfort profiles and current state.
  Credentials come from the environment so nothing is hard-coded:

      ECOBEE_EMAIL=you@example.com ECOBEE_PASSWORD=... python -m ecobee._selftest

  If the account has two-step verification the script prompts for the code on
  stdin, the same way the Homey pairing view asks for it.

Run from the app root:  python -m ecobee._selftest
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from . import api
from .auth import EcobeeAuth, MFACodeRequired, new_session
from .const import COMFORT_CUSTOM

_FAILURES: list[str] = []


def _check(label: str, got, want) -> None:
    if got == want:
        print(f"  [OK] {label}: {got!r}")
    else:
        print(f"  [!!] {label}: {got!r} (expected {want!r})")
        _FAILURES.append(label)


SAMPLE = {
    "identifier": "411811223344",
    "name": "Hallway",
    "equipmentStatus": "heatPump,fan",
    "runtime": {
        "actualTemperature": 706,  # 70.6 °F
        "actualHumidity": 38,
        "desiredHeat": 690,
        "desiredCool": 750,
    },
    "settings": {
        "hvacMode": "auto",
        "heatRangeLow": 450,
        "heatRangeHigh": 790,
        "coolRangeLow": 650,
        "coolRangeHigh": 920,
    },
    "program": {
        "currentClimateRef": "home",
        "climates": [
            {"climateRef": "home", "name": "Home"},
            {"climateRef": "away", "name": "Away"},
            {"climateRef": "sleep", "name": "Sleep"},
            {"climateRef": "smart1", "name": "Movie Night"},
        ],
    },
    "events": [],
    "remoteSensors": [
        {
            "id": "rs:100",
            "name": "Bedroom",
            "type": "ecobee3_remote_sensor",
            "inUse": True,
            "capability": [
                {"id": "1", "type": "temperature", "value": "721"},
                {"id": "2", "type": "occupancy", "value": "true"},
            ],
        },
        {
            "id": "ei:0",
            "name": "Hallway",
            "type": "thermostat",
            "inUse": True,
            "capability": [
                {"id": "1", "type": "temperature", "value": "706"},
                {"id": "2", "type": "humidity", "value": "38"},
                {"id": "3", "type": "occupancy", "value": "false"},
            ],
        },
        {
            "id": "rs:101",
            "name": "Office",
            "type": "ecobee3_remote_sensor",
            "inUse": True,
            "capability": [
                {"id": "1", "type": "temperature", "value": "unknown"},
                {"id": "2", "type": "occupancy", "value": "false"},
            ],
        },
    ],
}


def _with_events(events: list) -> dict:
    thermostat = json.loads(json.dumps(SAMPLE))
    thermostat["events"] = events
    return thermostat


def offline_checks() -> None:
    print("Temperature conversion")
    _check("706 tenths °F -> °C", round(api.f10_to_c(706), 2), 21.44)
    _check("21.5 °C -> tenths °F", api.c_to_f10(21.5), 710)
    _check("round trip 20 °C", round(api.f10_to_c(api.c_to_f10(20.0))), 20)

    print("No hold — the thermostat follows its schedule")
    thermostat = _with_events([])
    _check("hold status", api.hold_status_of(thermostat), "schedule")
    _check("comfort profile", api.comfort_profile_of(thermostat), "home")
    _check("fan mode", api.fan_mode_of(thermostat), "auto")
    _check("operating state", api.operating_state_of(thermostat), "heating")
    _check("thermostat mode", api.mode_of(thermostat), "auto")

    print("Hold on a comfort profile")
    thermostat = _with_events([{"type": "hold", "running": True, "holdClimateRef": "away"}])
    _check("hold status", api.hold_status_of(thermostat), "hold")
    _check("comfort profile", api.comfort_profile_of(thermostat), "away")

    print("ecobee's own auto-away event maps onto the Away profile")
    thermostat = _with_events([{"type": "autoAway", "running": True}])
    _check("comfort profile", api.comfort_profile_of(thermostat), "away")

    print("A bare setpoint hold belongs to no named profile")
    thermostat = _with_events(
        [{"type": "hold", "running": True, "heatHoldTemp": 700, "fan": "on"}]
    )
    _check("comfort profile", api.comfort_profile_of(thermostat), COMFORT_CUSTOM)
    _check("fan mode", api.fan_mode_of(thermostat), "on")

    print("Vacation")
    thermostat = _with_events([{"type": "vacation", "running": True, "name": "Ski"}])
    _check("hold status", api.hold_status_of(thermostat), "vacation")

    print("The running event wins over the ones queued behind it")
    thermostat = _with_events(
        [
            {"type": "hold", "running": False, "holdClimateRef": "sleep"},
            {"type": "hold", "running": True, "holdClimateRef": "away"},
        ]
    )
    _check("comfort profile", api.comfort_profile_of(thermostat), "away")

    print("Resolving a profile name typed into a flow card")
    _check("by display name", api.resolve_climate_ref(SAMPLE, "Movie Night"), "smart1")
    _check("by ref, any case", api.resolve_climate_ref(SAMPLE, "AWAY"), "away")
    _check("unknown name", api.resolve_climate_ref(SAMPLE, "Nope"), None)

    print("Which side of an auto band the dial should show")
    # Sample: heat 69 °F / cool 75 °F, indoor 70.6 °F, heat pump running.
    _check("heating equipment running", api.active_side_of(SAMPLE), "heat")
    thermostat = _with_events([])
    thermostat["equipmentStatus"] = "compCool1,fan"
    _check("cooling equipment running", api.active_side_of(thermostat), "cool")
    thermostat["equipmentStatus"] = ""
    thermostat["runtime"]["actualTemperature"] = 748  # 74.8 °F — hugging the cool side
    _check("idle near cool setpoint", api.active_side_of(thermostat), "cool")
    thermostat["runtime"]["actualTemperature"] = 700  # 70.0 °F — hugging the heat side
    _check("idle near heat setpoint", api.active_side_of(thermostat), "heat")

    print("Minimum heat/cool gap")
    _check("default when unreported", api.heat_cool_min_delta_f10(SAMPLE), 50)
    thermostat = _with_events([])
    thermostat["settings"]["heatCoolMinDelta"] = 30
    _check("thermostat's own value", api.heat_cool_min_delta_f10(thermostat), 30)

    print("Room sensors")
    sensors = api.remote_sensors_of(SAMPLE)
    _check("sensor count", len(sensors), 3)
    bedroom = api.find_remote_sensor(SAMPLE, "rs:100")
    _check("temperature °C", round(api.sensor_temperature_of(bedroom), 1), 22.3)
    _check("occupancy", api.sensor_occupancy_of(bedroom), True)
    _check("no humidity on a remote sensor", api.sensor_humidity_of(bedroom), None)
    _check("features", api.sensor_features_of(bedroom), ["temperature", "occupancy"])
    _check("label", api.sensor_label_of(bedroom, SAMPLE), "Bedroom (Hallway)")
    builtin = api.find_remote_sensor(SAMPLE, "ei:0")
    _check("built-in humidity", api.sensor_humidity_of(builtin), 38.0)
    _check(
        "built-in features",
        api.sensor_features_of(builtin),
        ["temperature", "occupancy", "humidity"],
    )
    _check("built-in label", api.sensor_label_of(builtin, SAMPLE), "Hallway (built-in sensor)")
    office = api.find_remote_sensor(SAMPLE, "rs:101")
    _check("offline sensor temperature", api.sensor_temperature_of(office), None)
    _check("offline sensor occupancy", api.sensor_occupancy_of(office), False)
    _check("unknown id", api.find_remote_sensor(SAMPLE, "rs:999"), None)

    print("Setpoints and slider bounds")
    heat, cool = api.setpoints_of(SAMPLE)
    _check("heat setpoint °C", round(heat, 1), 20.6)
    _check("cool setpoint °C", round(cool, 1), 23.9)
    low, high = api.setpoint_range_of(SAMPLE)
    _check("slider min °C", round(low), 7)
    _check("slider max °C", round(high), 33)


async def live_check(email: str, password: str) -> None:
    async with new_session() as http:
        try:
            auth = await EcobeeAuth.login(http, email, password)
        except MFACodeRequired as e:
            code = input(f"  ecobee wants a {e.challenge_type} code: ").strip()
            auth = await EcobeeAuth.resume_with_mfa_code(
                http,
                prompt_url=e.prompt_url,
                state=e.state,
                code=code,
                verifier=e.verifier,
                email=e.email,
            )
        print("  [OK] signed in; refresh token acquired")

        client = api.EcobeeClient(auth, http)
        thermostats = await client.get_thermostats()
        print(f"  [OK] {len(thermostats)} thermostat(s) on the account")
        for thermostat in thermostats:
            print(f"\n  {api.label_of(thermostat)}  ({api.identifier_of(thermostat)})")
            print(f"    mode            {api.mode_of(thermostat)}")
            print(f"    indoor          {api.indoor_temperature_of(thermostat):.1f} °C"
                  f" / {api.indoor_humidity_of(thermostat):.0f}% RH")
            heat, cool = api.setpoints_of(thermostat)
            print(f"    setpoints       heat {heat:.1f} °C / cool {cool:.1f} °C")
            print(f"    comfort profile {api.comfort_profile_of(thermostat)}")
            print(f"    hold status     {api.hold_status_of(thermostat)}")
            print(f"    running         {api.operating_state_of(thermostat)}")
            profiles = ", ".join(
                f"{c['name']} ({c['ref']})" for c in api.climates_of(thermostat)
            )
            print(f"    profiles        {profiles}")
            for sensor in api.remote_sensors_of(thermostat):
                temperature = api.sensor_temperature_of(sensor)
                shown = f"{temperature:.1f} °C" if temperature is not None else "unknown"
                occupancy = api.sensor_occupancy_of(sensor)
                motion = {True: "motion", False: "clear", None: "n/a"}[occupancy]
                print(
                    f"    sensor          {api.sensor_label_of(sensor, thermostat)}: "
                    f"{shown}, {motion}"
                )


def main() -> int:
    print("OFFLINE checks")
    offline_checks()
    if _FAILURES:
        print(f"\n{len(_FAILURES)} offline check(s) FAILED: {', '.join(_FAILURES)}")
        return 1
    print("\nAll offline checks passed.")

    email = os.environ.get("ECOBEE_EMAIL")
    password = os.environ.get("ECOBEE_PASSWORD")
    if not (email and password):
        print("LIVE check skipped (set ECOBEE_EMAIL / ECOBEE_PASSWORD to run it)")
        return 0

    print("\nLIVE check")
    try:
        asyncio.run(live_check(email, password))
    except Exception as e:  # noqa: BLE001 - this is a diagnostic script
        print(f"  [!!] live check failed: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
