"""ecobee REST client plus the helpers that read a thermostat's JSON.

Two ecobee quirks drive the shape of this module:

* GET passes its whole `selection` object as a URL-encoded JSON string in a
  `json=` query parameter. POST sends a JSON body instead, carrying either a
  `thermostat` patch (direct settings) or a `functions` array (setHold,
  resumeProgram, …). Never both.
* Every response is wrapped in a `status` envelope; HTTP 200 with a non-zero
  `status.code` is still a failure.

Temperatures on the wire are always tenths of a degree Fahrenheit, whatever the
thermostat displays. Homey works in Celsius, so everything is converted at this
boundary.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

import aiohttp

from .auth import EcobeeAuth, EcobeeAuthError, EcobeeError, InvalidGrant
from .const import (
    API_THERMOSTAT,
    AUTH_ERROR_CODES,
    AUTO_EVENT_CLIMATE,
    COMFORT_CUSTOM,
    COMFORT_SCHEDULE,
    DEFAULT_HOLD_TYPE,
    EQUIPMENT_TO_STATE,
    HOLD_HOLD,
    HOLD_SCHEDULE,
    HOLD_VACATION,
    MODE_API_TO_HOMEY,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=25)

# Everything the device layer might need, fetched in one round trip. ecobee
# charges the same for a fat selection as a thin one.
_POLL_SELECTION = {
    "selection": {
        "selectionType": "registered",
        "selectionMatch": "",
        "includeRuntime": True,
        "includeProgram": True,
        "includeEvents": True,
        "includeEquipmentStatus": True,
        "includeSettings": True,
        "includeWeather": False,
        "includeSensors": True,
    }
}


class EcobeeAPIError(EcobeeError):
    """The API call failed (bad status, malformed body, network)."""


class EcobeeRateLimitError(EcobeeAPIError):
    """HTTP 429 — back off and retry on the next poll."""


class EcobeeRequestRejected(EcobeeAPIError):
    """ecobee answered, but refused the request (validation error, bad value…).

    Distinct from a network failure: the call reached ecobee and nothing was
    changed, so callers can say so with confidence.
    """


# ─────────────────────────────────────────────
# Temperature conversion
# ─────────────────────────────────────────────

def f10_to_c(value: Any) -> float | None:
    """Tenths of °F (ecobee's wire unit) -> °C."""
    if not isinstance(value, (int, float)):
        return None
    return ((float(value) / 10.0) - 32.0) * 5.0 / 9.0


def c_to_f10(celsius: float) -> int:
    """°C -> tenths of °F, rounded to whole degrees F as ecobee's UI does."""
    return int(round(float(celsius) * 9.0 / 5.0 + 32.0)) * 10


# ─────────────────────────────────────────────
# REST client
# ─────────────────────────────────────────────

class EcobeeClient:
    """Thin async client over /1/thermostat."""

    def __init__(self, auth: EcobeeAuth, session: aiohttp.ClientSession) -> None:
        self._auth = auth
        self._session = session

    # ── low-level ──────────────────────────────────────────────

    async def _bearer(self) -> str:
        try:
            return await self._auth.ensure_valid()
        except InvalidGrant as e:
            raise EcobeeAuthError(str(e)) from e

    def _check_envelope(self, payload: dict) -> None:
        status = payload.get("status") or {}
        code = status.get("code", 0)
        if code == 0:
            return
        message = status.get("message", "(no message)")
        if code in AUTH_ERROR_CODES:
            raise EcobeeAuthError(f"ecobee rejected the token (code {code}): {message}")
        raise EcobeeRequestRejected(f"ecobee error code {code}: {message}")

    async def _read(self, r: aiohttp.ClientResponse, what: str) -> dict:
        if r.status == 401:
            raise EcobeeAuthError(f"{what} -> 401 unauthorized")
        if r.status == 429:
            raise EcobeeRateLimitError(f"{what} -> 429 rate limited")
        text = await r.text()
        if r.status != 200:
            # ecobee reports validation failures as a non-200 with a status
            # envelope; surface its message when there is one.
            try:
                status = json.loads(text).get("status") or {}
            except (json.JSONDecodeError, AttributeError):
                status = {}
            raise EcobeeRequestRejected(
                f"{what} -> {r.status}: {status.get('message') or text[:200]}"
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise EcobeeAPIError(f"{what}: malformed JSON ({e}): {text[:200]!r}") from e
        self._check_envelope(payload)
        return payload

    # ── read ───────────────────────────────────────────────────

    async def get_thermostats(self) -> list[dict]:
        """Return the raw thermostatList, unprocessed."""
        token = await self._bearer()
        url = f"{API_THERMOSTAT}?json=" + urllib.parse.quote(
            json.dumps(_POLL_SELECTION, separators=(",", ":"))
        )
        try:
            async with self._session.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            ) as r:
                payload = await self._read(r, "GET /thermostat")
        except aiohttp.ClientError as e:
            raise EcobeeAPIError(f"GET /thermostat network error: {e}") from e
        thermostats = payload.get("thermostatList")
        return thermostats if isinstance(thermostats, list) else []

    async def get_thermostat(self, identifier: str) -> dict | None:
        """Return one thermostat by identifier, or None if it is gone."""
        for t in await self.get_thermostats():
            if str(t.get("identifier")) == str(identifier):
                return t
        return None

    # ── write ──────────────────────────────────────────────────

    async def _post(self, body: dict) -> None:
        token = await self._bearer()
        try:
            async with self._session.post(
                f"{API_THERMOSTAT}?format=json",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json;charset=UTF-8",
                    "Accept": "application/json",
                },
                json=body,
                timeout=REQUEST_TIMEOUT,
            ) as r:
                await self._read(r, "POST /thermostat")
        except aiohttp.ClientError as e:
            raise EcobeeAPIError(f"POST /thermostat network error: {e}") from e

    @staticmethod
    def _selection(identifier: str) -> dict:
        return {"selectionType": "thermostats", "selectionMatch": str(identifier)}

    async def get_hvac_mode(self, identifier: str) -> str | None:
        """The thermostat's raw `settings.hvacMode`, fetched fresh.

        Unlike the poll this skips the device cache, so a flow card deciding
        what to save or restore never acts on a reading up to a poll old. The
        selection is just one thermostat's settings.
        """
        token = await self._bearer()
        selection = {
            "selection": {
                **self._selection(identifier),
                "includeSettings": True,
            }
        }
        url = f"{API_THERMOSTAT}?json=" + urllib.parse.quote(
            json.dumps(selection, separators=(",", ":"))
        )
        try:
            async with self._session.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            ) as r:
                payload = await self._read(r, "GET /thermostat (settings)")
        except aiohttp.ClientError as e:
            raise EcobeeAPIError(f"GET /thermostat network error: {e}") from e
        for t in payload.get("thermostatList") or []:
            if isinstance(t, dict) and str(t.get("identifier")) == str(identifier):
                return hvac_mode_of(t)
        return None

    async def update_settings(self, identifier: str, settings: dict) -> None:
        """Patch the thermostat's `settings` object (hvacMode, fan runtime, …)."""
        await self._post(
            {
                "selection": self._selection(identifier),
                "thermostat": {"settings": settings},
            }
        )

    async def set_hold(
        self,
        identifier: str,
        *,
        climate_ref: str | None = None,
        heat_f10: int | None = None,
        cool_f10: int | None = None,
        fan: str | None = None,
        hold_type: str = DEFAULT_HOLD_TYPE,
        hold_hours: int | None = None,
    ) -> None:
        """Create a hold — by comfort profile, by setpoint, or both.

        A `climate_ref` ('home', 'away', 'sleep', or a custom climate's ref)
        pulls that profile's setpoints and fan mode; anything passed alongside it
        is overridden by the profile's own values, which is exactly what makes
        "set the thermostat to Away" a single call.
        """
        params: dict[str, Any] = {"holdType": hold_type}
        if hold_type == "holdHours":
            params["holdHours"] = int(hold_hours or 2)
        if climate_ref is not None:
            params["holdClimateRef"] = climate_ref
        if heat_f10 is not None:
            params["heatHoldTemp"] = int(heat_f10)
        if cool_f10 is not None:
            params["coolHoldTemp"] = int(cool_f10)
        if fan is not None:
            params["fan"] = fan
        await self._post(
            {
                "selection": self._selection(identifier),
                "functions": [{"type": "setHold", "params": params}],
            }
        )

    async def resume_program(self, identifier: str, *, resume_all: bool = True) -> None:
        """Drop holds and go back to the schedule.

        `resume_all` clears the whole stack — without it one tap can pop a single
        hold and leave another one underneath, which reads as "nothing happened".
        """
        await self._post(
            {
                "selection": self._selection(identifier),
                "functions": [
                    {"type": "resumeProgram", "params": {"resumeAll": bool(resume_all)}}
                ],
            }
        )


# ─────────────────────────────────────────────
# Thermostat JSON readers
# ─────────────────────────────────────────────

def identifier_of(thermostat: dict) -> str:
    return str(thermostat.get("identifier") or "")


def label_of(thermostat: dict) -> str:
    return str(thermostat.get("name") or identifier_of(thermostat) or "Thermostat")


def active_event(thermostat: dict) -> dict | None:
    """The event currently in force, if any.

    ecobee returns events in priority order and marks the effective one with
    `running`. Some firmwares omit the flag, so fall back to the first entry.
    """
    events = thermostat.get("events") or []
    if not isinstance(events, list) or not events:
        return None
    for event in events:
        if isinstance(event, dict) and event.get("running"):
            return event
    first = events[0]
    return first if isinstance(first, dict) else None


def hold_status_of(thermostat: dict) -> str:
    """`schedule`, `hold`, or `vacation`."""
    event = active_event(thermostat)
    if not event:
        return HOLD_SCHEDULE
    event_type = event.get("type")
    if event_type == "vacation":
        return HOLD_VACATION
    if event_type in ("hold", "quickSave", "autoAway", "autoHome", "demandResponse"):
        return HOLD_HOLD
    return HOLD_SCHEDULE


def comfort_profile_of(thermostat: dict) -> str:
    """Which comfort profile is in effect right now.

    With no hold running that is simply the scheduled climate. Under a hold it is
    the held climate — `holdClimateRef` for a profile hold, a mapped name for
    ecobee's own auto-away/auto-home events, and `custom` for a bare setpoint
    hold or vacation, which belong to no named profile.
    """
    event = active_event(thermostat)
    if event and hold_status_of(thermostat) != HOLD_SCHEDULE:
        climate_ref = event.get("holdClimateRef")
        if climate_ref:
            return str(climate_ref)
        mapped = AUTO_EVENT_CLIMATE.get(str(event.get("type")))
        return mapped or COMFORT_CUSTOM
    current = (thermostat.get("program") or {}).get("currentClimateRef")
    return str(current) if current else COMFORT_SCHEDULE


def climates_of(thermostat: dict) -> list[dict]:
    """Every comfort profile defined on the thermostat, custom ones included.

    Each entry is `{"ref": …, "name": …}` — `ref` is what setHold wants, `name`
    is what the user named it in the ecobee app.
    """
    program = thermostat.get("program") or {}
    out: list[dict] = []
    for climate in program.get("climates") or []:
        if not isinstance(climate, dict):
            continue
        ref = climate.get("climateRef")
        if not ref:
            continue
        out.append({"ref": str(ref), "name": str(climate.get("name") or ref)})
    return out


def resolve_climate_ref(thermostat: dict, wanted: str) -> str | None:
    """Match a user-typed profile name against the thermostat's climates.

    Accepts either the ref ('smartAway1') or the display name ('Movie Night'),
    case-insensitively, so a flow card can take plain text.
    """
    needle = (wanted or "").strip().lower()
    if not needle:
        return None
    climates = climates_of(thermostat)
    for climate in climates:
        if climate["ref"].lower() == needle:
            return climate["ref"]
    for climate in climates:
        if climate["name"].lower() == needle:
            return climate["ref"]
    return None


def mode_of(thermostat: dict) -> str | None:
    """The Homey thermostat_mode slug for the thermostat's hvacMode."""
    hvac_mode = (thermostat.get("settings") or {}).get("hvacMode")
    return MODE_API_TO_HOMEY.get(str(hvac_mode)) if hvac_mode else None


def hvac_mode_of(thermostat: dict) -> str | None:
    """The raw ecobee hvacMode ('heat', 'auxHeatOnly', …), unmapped.

    `mode_of` folds auxHeatOnly into Homey's `heat`; the aux-heat flow cards
    need to tell the two apart.
    """
    hvac_mode = (thermostat.get("settings") or {}).get("hvacMode")
    return str(hvac_mode) if hvac_mode else None


def fan_mode_of(thermostat: dict) -> str:
    """`on` while a hold forces the fan, `auto` otherwise.

    ecobee has no persistent fan-mode setting: continuous fan is a property of
    the running event, so with no hold in force the fan is always on auto.
    """
    event = active_event(thermostat)
    if event and str(event.get("fan") or "").lower() == "on":
        return "on"
    return "auto"


def operating_state_of(thermostat: dict) -> str:
    """What the HVAC is doing: heating / cooling / fan / idle.

    equipmentStatus tokens are stage-numbered (`compCool1`, `auxHeat2`), so each
    is matched by prefix against the table rather than exactly.
    """
    equipment = str(thermostat.get("equipmentStatus") or "")
    running = [part.strip() for part in equipment.split(",") if part.strip()]
    for key, state in EQUIPMENT_TO_STATE:
        if any(token.startswith(key) for token in running):
            return state
    return "idle"


def setpoints_of(thermostat: dict) -> tuple[float | None, float | None]:
    """(heat, cool) setpoints in Celsius, as the thermostat is currently holding."""
    runtime = thermostat.get("runtime") or {}
    return f10_to_c(runtime.get("desiredHeat")), f10_to_c(runtime.get("desiredCool"))


def indoor_temperature_of(thermostat: dict) -> float | None:
    return f10_to_c((thermostat.get("runtime") or {}).get("actualTemperature"))


def indoor_humidity_of(thermostat: dict) -> float | None:
    humidity = (thermostat.get("runtime") or {}).get("actualHumidity")
    return float(humidity) if isinstance(humidity, (int, float)) else None


# ─────────────────────────────────────────────
# Remote sensors (SmartSensors + the thermostat's built-in one)
# ─────────────────────────────────────────────
# remoteSensors entries carry a `capability` list whose values are strings:
# temperature in tenths of °F ("706"), occupancy "true"/"false", humidity "51".
# A sensor that has dropped offline reports "unknown".

def remote_sensors_of(thermostat: dict) -> list[dict]:
    sensors = thermostat.get("remoteSensors")
    return [s for s in sensors if isinstance(s, dict)] if isinstance(sensors, list) else []


def find_remote_sensor(thermostat: dict, sensor_id: str) -> dict | None:
    for sensor in remote_sensors_of(thermostat):
        if str(sensor.get("id")) == str(sensor_id):
            return sensor
    return None


def _sensor_value(sensor: dict, cap_type: str) -> str | None:
    for cap in sensor.get("capability") or []:
        if isinstance(cap, dict) and cap.get("type") == cap_type:
            value = cap.get("value")
            return None if value in (None, "", "unknown") else str(value)
    return None


def sensor_temperature_of(sensor: dict) -> float | None:
    value = _sensor_value(sensor, "temperature")
    try:
        return f10_to_c(float(value)) if value is not None else None
    except ValueError:
        return None


def sensor_occupancy_of(sensor: dict) -> bool | None:
    value = _sensor_value(sensor, "occupancy")
    if value is None:
        return None
    return value.strip().lower() == "true"


def sensor_humidity_of(sensor: dict) -> float | None:
    value = _sensor_value(sensor, "humidity")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def sensor_features_of(sensor: dict) -> list[str]:
    """Which capability types this sensor carries, in a stable order."""
    present = {
        cap.get("type")
        for cap in sensor.get("capability") or []
        if isinstance(cap, dict)
    }
    return [f for f in ("temperature", "occupancy", "humidity") if f in present]


def sensor_label_of(sensor: dict, thermostat: dict) -> str:
    """Pairing label: the sensor's name, disambiguated by its thermostat.

    The thermostat's built-in sensor is named after the thermostat itself, so it
    gets a suffix instead of a duplicate-looking entry.
    """
    name = str(sensor.get("name") or sensor.get("id") or "Sensor")
    if str(sensor.get("type")) == "thermostat":
        return f"{name} (built-in sensor)"
    return f"{name} ({label_of(thermostat)})"


def active_side_of(thermostat: dict) -> str:
    """Which side of an auto band is in play: `heat` or `cool`.

    Running equipment is definitive; when idle, the side whose setpoint sits
    closer to the indoor temperature is the one the thermostat will act on
    next. Matches what the ecobee's own display leads with.
    """
    state = operating_state_of(thermostat)
    if state == "cooling":
        return "cool"
    if state == "heating":
        return "heat"
    heat, cool = setpoints_of(thermostat)
    if cool is None:
        return "heat"
    if heat is None:
        return "cool"
    indoor = indoor_temperature_of(thermostat)
    if indoor is None:
        return "cool"
    return "cool" if abs(cool - indoor) <= abs(indoor - heat) else "heat"


def heat_cool_min_delta_f10(thermostat: dict) -> int:
    """The smallest heat/cool gap the thermostat allows, in tenths of °F.

    ecobee rejects auto-mode holds whose band is narrower than this
    (`settings.heatCoolMinDelta`; 5 °F when unreported).
    """
    delta = (thermostat.get("settings") or {}).get("heatCoolMinDelta")
    return int(delta) if isinstance(delta, (int, float)) and delta > 0 else 50


def setpoint_range_of(thermostat: dict) -> tuple[float, float]:
    """The thermostat's own setpoint limits, in Celsius.

    ecobee reports these per mode; the widest pair is used so the Homey slider
    never refuses a value the thermostat would have accepted.
    """
    settings = thermostat.get("settings") or {}
    lows = [
        f10_to_c(settings.get("heatRangeLow")),
        f10_to_c(settings.get("coolRangeLow")),
    ]
    highs = [
        f10_to_c(settings.get("heatRangeHigh")),
        f10_to_c(settings.get("coolRangeHigh")),
    ]
    valid_lows = [v for v in lows if v is not None]
    valid_highs = [v for v in highs if v is not None]
    low = min(valid_lows) if valid_lows else 5.0
    high = max(valid_highs) if valid_highs else 35.0
    return low, high
