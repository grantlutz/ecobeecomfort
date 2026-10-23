"""ecobee thermostat device — cloud polling, capability mapping, and commands.

ecobee is cloud-only, so state comes from a poll of /1/thermostat every few
minutes and commands go back as `setHold` / `resumeProgram` functions or direct
`settings` patches.

The interesting capability is `ecobee_comfort`: picking Home, Away or Sleep
creates an indefinite hold on that comfort profile, and picking Schedule resumes
the thermostat's own program. That is the piece the other ecobee apps leave out.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

# Make the app-root `ecobee` package importable. NOTE: it is named `ecobee`, not
# `lib`, because the Homey runner already owns a top-level `lib` package —
# `from lib.x import` would resolve to the runner's and fail.
_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from homey.device import Device  # noqa: E402

from ecobee.api import (  # noqa: E402
    EcobeeAPIError,
    EcobeeClient,
    EcobeeRateLimitError,
    EcobeeRequestRejected,
    active_side_of,
    c_to_f10,
    comfort_profile_of,
    heat_cool_min_delta_f10,
    fan_mode_of,
    hold_status_of,
    indoor_humidity_of,
    indoor_temperature_of,
    mode_of,
    operating_state_of,
    resolve_climate_ref,
    setpoint_range_of,
    setpoints_of,
)
from ecobee import registry  # noqa: E402
from ecobee.auth import (  # noqa: E402
    EcobeeAuth,
    EcobeeAuthError,
    EcobeeError,
    new_session,
)
from ecobee.const import (  # noqa: E402
    AUX_RESTORE_FALLBACK_MODE,
    COMFORT_CUSTOM,
    COMFORT_SCHEDULE,
    DEFAULT_HOLD_TYPE,
    DEFAULT_POLL_INTERVAL,
    HOMEY_MODE_AUX,
    HVAC_MODE_AUX_HEAT_ONLY,
    MODE_API_TO_HOMEY,
    MODE_HOMEY_TO_API,
    PREV_HVAC_MODE_STORE_KEY,
    STANDARD_CLIMATES,
    THERMOSTAT_MODE_VALUES,
)

CAPABILITIES = (
    "measure_temperature",
    "measure_humidity",
    "target_temperature",
    "thermostat_mode",
    "ecobee_comfort",
    "ecobee_fan_mode",
    "ecobee_hold_status",
)


class EcobeeThermostatDevice(Device):
    # ── Lifecycle ──────────────────────────────────────────────────────────
    async def on_init(self) -> None:
        store = self.get_store()
        self.identifier = store.get("identifier") or self.get_data().get("id")

        self._loop = asyncio.get_running_loop()
        self._shutting_down = False
        self._poll_task: asyncio.Task | None = None
        self._thermostat: dict = {}      # last full poll payload
        self._bounds_cache: dict | None = None
        self._last_comfort: str | None = None

        self._session = new_session()
        self._auth = EcobeeAuth.from_dict(store, self._session)
        self._client = EcobeeClient(self._auth, self._session)

        # Ensure devices paired against an older version pick up new capabilities.
        for cap in CAPABILITIES:
            if not self.has_capability(cap):
                try:
                    await self.add_capability(cap)
                except Exception as e:  # noqa: BLE001
                    self.error(f"add_capability {cap} failed:", e)
        await self._migrate_mode_values()

        self.register_capability_listener("target_temperature", self._on_set_target)
        self.register_capability_listener("thermostat_mode", self._on_set_mode)
        self.register_capability_listener("ecobee_comfort", self._on_set_comfort)
        self.register_capability_listener("ecobee_fan_mode", self._on_set_fan)

        try:
            await self._auth.ensure_valid()
            await self._persist_tokens()
        except EcobeeError as e:
            self.error("Initial sign-in failed:", e)
            await self.set_unavailable("Sign-in failed — please re-pair the thermostat.")
            return

        await self._poll()
        self._poll_task = self._loop.create_task(self._poll_loop())
        self.log("ecobee thermostat initialized:", self.identifier)

    async def on_uninit(self) -> None:
        self._shutting_down = True
        if self._poll_task:
            self._poll_task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    async def on_deleted(self) -> None:
        await self.on_uninit()

    async def _migrate_mode_values(self) -> None:
        """Give devices paired before `aux` existed the current mode values.

        Only writes when the picker is missing a value, since setting
        capability options is expensive.
        """
        try:
            options = dict(self.get_capability_options("thermostat_mode"))
        except Exception as e:  # noqa: BLE001
            self.error("read thermostat_mode options failed:", e)
            return
        have = {v.get("id") for v in options.get("values") or [] if isinstance(v, dict)}
        if all(v["id"] in have for v in THERMOSTAT_MODE_VALUES):
            return
        options["values"] = [dict(v) for v in THERMOSTAT_MODE_VALUES]
        try:
            await self.set_capability_options("thermostat_mode", options)
            self.log("thermostat_mode values updated (added aux heat)")
        except Exception as e:  # noqa: BLE001
            self.error("set thermostat_mode options failed:", e)

    # ── Token persistence ───────────────────────────────────────────────────
    async def _persist_tokens(self) -> None:
        """Write a rotated refresh token back so a restart stays signed in."""
        if not self._auth.tokens_rotated:
            return
        try:
            await self.set_store_value("refresh_token", self._auth.refresh_token)
            self._auth.tokens_rotated = False
        except Exception as e:  # noqa: BLE001
            self.error("persist tokens failed:", e)

    # ── Polling ──────────────────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        while not self._shutting_down:
            try:
                await asyncio.sleep(DEFAULT_POLL_INTERVAL)
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.error("poll loop error:", e)

    async def _poll(self) -> None:
        try:
            data = await self._client.get_thermostat(self.identifier)
            await self._persist_tokens()
        except EcobeeRateLimitError as e:
            self.log("Rate limited on poll; retrying next cycle:", e)
            return
        except EcobeeAuthError as e:
            self.error("Auth error on poll:", e)
            await self.set_unavailable("Sign-in expired — please re-pair the thermostat.")
            return
        except EcobeeError as e:
            self.error("Poll error:", e)
            return
        if not data:
            self.error("Thermostat", self.identifier, "is not on this ecobee account")
            await self.set_unavailable("This thermostat is no longer on the ecobee account.")
            return
        await self.set_available()
        await self._apply_state(data)

    async def _apply_state(self, data: dict) -> None:
        self._thermostat = data

        # Feed the room-sensor devices that ride on this thermostat's poll.
        registry.set_latest(self.identifier, data)
        for sensor_device in registry.sensors_for(self.identifier):
            try:
                await sensor_device.apply_thermostat(data)
            except Exception as e:  # noqa: BLE001
                self.error("room sensor update failed:", e)

        temperature = indoor_temperature_of(data)
        if temperature is not None:
            await self._safe_set("measure_temperature", round(temperature, 1))
        humidity = indoor_humidity_of(data)
        if humidity is not None:
            await self._safe_set("measure_humidity", round(humidity))

        homey_mode = mode_of(data)
        if homey_mode:
            await self._safe_set("thermostat_mode", homey_mode)

        await self._apply_target_bounds(data)
        target = self._active_setpoint(data, homey_mode)
        if target is not None:
            await self._safe_set("target_temperature", round(target * 2) / 2)

        comfort = comfort_profile_of(data)
        await self._safe_set("ecobee_comfort", self._comfort_capability_value(data, comfort))
        await self._safe_set("ecobee_hold_status", hold_status_of(data))
        await self._safe_set("ecobee_fan_mode", fan_mode_of(data))

        await self._fire_triggers(comfort)

    def _comfort_capability_value(self, data: dict, comfort: str) -> str:
        """Map the live comfort profile onto the picker's fixed value set.

        The picker can only offer the three stock climates plus Schedule, so a
        custom climate — or a plain setpoint hold that belongs to no profile at
        all — is reported as `custom` rather than silently showing the wrong one.
        """
        if hold_status_of(data) == "schedule" and comfort not in STANDARD_CLIMATES:
            return COMFORT_SCHEDULE
        if comfort in STANDARD_CLIMATES:
            return comfort
        return COMFORT_CUSTOM

    async def _apply_target_bounds(self, data: dict) -> None:
        """Track the thermostat's own setpoint limits on the Homey slider."""
        low, high = setpoint_range_of(data)
        opts = {
            "min": round(low * 2) / 2,
            "max": round(high * 2) / 2,
            "step": 0.5,
            "decimals": 0,
        }
        if self._bounds_cache == opts:
            return
        try:
            await self.set_capability_options("target_temperature", opts)
            self._bounds_cache = opts
        except Exception as e:  # noqa: BLE001
            self.error("set target bounds failed:", e)

    def _active_setpoint(self, data: dict, homey_mode: str | None) -> float | None:
        """The setpoint the slider should show for the current mode.

        In auto the thermostat holds a heat/cool band rather than one number.
        Showing the band's midpoint matches nothing on the ecobee's own screen,
        so the slider shows the side that is actually in play — cooling on a
        summer afternoon, heating on a winter night.
        """
        heat, cool = setpoints_of(data)
        if homey_mode == "cool":
            return cool
        if homey_mode in ("heat", HOMEY_MODE_AUX):
            return heat
        return cool if active_side_of(data) == "cool" else heat

    # ── Commands ─────────────────────────────────────────────────────────────
    async def _refreshed(self) -> dict:
        """The freshest thermostat payload, refetched if we have none cached."""
        if not self._thermostat:
            data = await self._client.get_thermostat(self.identifier)
            if data:
                self._thermostat = data
        return self._thermostat

    async def async_set_target(self, celsius: float) -> None:
        """Hold a setpoint.

        The slider edits whichever setpoint it is displaying — the mode's own in
        heat/cool, the in-play side in auto. In auto the untouched side follows
        only as far as ecobee's minimum heat/cool gap forces it to.
        """
        data = await self._refreshed()
        homey_mode = mode_of(data) or "heat"
        heat, cool = setpoints_of(data)
        delta_f10 = heat_cool_min_delta_f10(data)

        if homey_mode == "cool":
            side = "cool"
        elif homey_mode in ("heat", HOMEY_MODE_AUX):
            side = "heat"
        else:
            side = active_side_of(data)

        if side == "cool":
            cool_f10 = c_to_f10(celsius)
            heat_f10 = c_to_f10(heat) if heat is not None else cool_f10 - delta_f10
            if homey_mode == "auto" and cool_f10 - heat_f10 < delta_f10:
                heat_f10 = cool_f10 - delta_f10
        else:
            heat_f10 = c_to_f10(celsius)
            cool_f10 = c_to_f10(cool) if cool is not None else heat_f10 + delta_f10
            if homey_mode == "auto" and cool_f10 - heat_f10 < delta_f10:
                cool_f10 = heat_f10 + delta_f10

        await self._client.set_hold(
            self.identifier,
            heat_f10=heat_f10,
            cool_f10=cool_f10,
            hold_type=DEFAULT_HOLD_TYPE,
        )
        await self._persist_tokens()
        await self._safe_set("ecobee_hold_status", "hold")

    async def async_set_mode(self, homey_mode: str) -> None:
        if homey_mode == HOMEY_MODE_AUX:
            await self.async_set_aux_heat_only()
            return
        api_mode = MODE_HOMEY_TO_API.get(homey_mode)
        if not api_mode:
            raise Exception(f"Unknown thermostat mode: {homey_mode}")
        await self._write_hvac_mode(api_mode)
        # A mode picked by hand replaces aux heat only for good, so there is
        # nothing left for the restore card to put back.
        if PREV_HVAC_MODE_STORE_KEY in self.get_store():
            await self.unset_store_value(PREV_HVAC_MODE_STORE_KEY)

    # ── Aux heat only ────────────────────────────────────────────────────────
    # Reached from the "Set aux heat only" card and from picking Aux heat in
    # the thermostat_mode picker, so a Flow can switch to it (say, during
    # electricity peak hours) and later put back exactly the mode it replaced.

    async def _fresh_hvac_mode(self) -> str:
        """The raw hvacMode straight from ecobee, bypassing the poll cache."""
        hvac_mode = await self._client.get_hvac_mode(self.identifier)
        await self._persist_tokens()
        if not hvac_mode:
            raise Exception("Could not read the thermostat's current mode from ecobee.")
        return hvac_mode

    async def _write_hvac_mode(self, hvac_mode: str) -> None:
        """Set a raw hvacMode and reflect it locally until the next poll."""
        await self._client.update_settings(self.identifier, {"hvacMode": hvac_mode})
        await self._persist_tokens()
        if self._thermostat:
            self._thermostat.setdefault("settings", {})["hvacMode"] = hvac_mode
        homey_mode = MODE_API_TO_HOMEY.get(hvac_mode)
        if homey_mode:
            await self._safe_set("thermostat_mode", homey_mode)  # optimistic

    async def async_set_aux_heat_only(self) -> None:
        """Switch to aux heat only, remembering the mode it replaces.

        Already in aux heat only: nothing to do, and the mode saved when it was
        switched on is kept so the restore card still has it.
        """
        current = await self._fresh_hvac_mode()
        if current == HVAC_MODE_AUX_HEAT_ONLY:
            return
        await self.set_store_value(PREV_HVAC_MODE_STORE_KEY, current)
        try:
            await self._write_hvac_mode(HVAC_MODE_AUX_HEAT_ONLY)
        except EcobeeRequestRejected as e:
            # ecobee answered and changed nothing, so there is nothing to restore.
            await self.unset_store_value(PREV_HVAC_MODE_STORE_KEY)
            self.error("aux heat only rejected:", e)
            raise Exception(
                "ecobee did not accept aux heat only mode. Check that this "
                "thermostat is set up with auxiliary (backup) heat in its "
                f"installation settings. ecobee said: {e}"
            ) from e

    async def async_restore_from_aux_heat(self) -> None:
        """Put back the mode saved by `async_set_aux_heat_only`.

        Only acts while the thermostat is still in aux heat only: if someone
        changed the mode in the meantime their choice wins and the saved mode
        is discarded. With nothing saved (aux heat was switched on outside
        Homey) it falls back to heat.
        """
        saved = self.get_store().get(PREV_HVAC_MODE_STORE_KEY)
        current = await self._fresh_hvac_mode()
        if current != HVAC_MODE_AUX_HEAT_ONLY:
            if saved is not None:
                await self.unset_store_value(PREV_HVAC_MODE_STORE_KEY)
            return
        if not saved or saved == HVAC_MODE_AUX_HEAT_ONLY:
            saved = AUX_RESTORE_FALLBACK_MODE
        await self._write_hvac_mode(str(saved))
        await self.unset_store_value(PREV_HVAC_MODE_STORE_KEY)

    async def async_is_aux_heat_only(self) -> bool:
        """True when the thermostat's raw hvacMode is aux heat only."""
        return await self._fresh_hvac_mode() == HVAC_MODE_AUX_HEAT_ONLY

    async def async_set_comfort(
        self, profile: str, *, hold_type: str = DEFAULT_HOLD_TYPE, hold_hours: int | None = None
    ) -> None:
        """Hold a comfort profile, or resume the schedule.

        `profile` is a climate ref ('home', 'away', 'sleep', or a custom one) or
        the sentinel `schedule`, which clears holds instead of adding one.
        """
        if profile == COMFORT_SCHEDULE:
            await self.async_resume_schedule()
            return
        if profile == COMFORT_CUSTOM:
            raise Exception(
                "'Custom' shows what the thermostat is doing — pick a named "
                "profile, or use the 'set comfort profile by name' flow card."
            )
        await self._client.set_hold(
            self.identifier,
            climate_ref=profile,
            hold_type=hold_type,
            hold_hours=hold_hours,
        )
        await self._persist_tokens()
        await self._safe_set(
            "ecobee_comfort", profile if profile in STANDARD_CLIMATES else COMFORT_CUSTOM
        )
        await self._safe_set("ecobee_hold_status", "hold")

    async def async_set_comfort_by_name(
        self, name: str, *, hold_type: str = DEFAULT_HOLD_TYPE, hold_hours: int | None = None
    ) -> None:
        """Hold a comfort profile named in a flow card, custom ones included."""
        data = await self._refreshed()
        ref = resolve_climate_ref(data, name)
        if not ref:
            from ecobee.api import climates_of

            known = ", ".join(c["name"] for c in climates_of(data)) or "(none found)"
            raise Exception(f"No comfort profile called '{name}'. This thermostat has: {known}")
        await self.async_set_comfort(ref, hold_type=hold_type, hold_hours=hold_hours)

    async def async_resume_schedule(self) -> None:
        await self._client.resume_program(self.identifier, resume_all=True)
        await self._persist_tokens()
        await self._safe_set("ecobee_hold_status", "schedule")
        # The scheduled climate is authoritative again, so show it right away
        # rather than waiting out the poll interval.
        data = self._thermostat
        if data:
            scheduled = (data.get("program") or {}).get("currentClimateRef")
            if scheduled in STANDARD_CLIMATES:
                await self._safe_set("ecobee_comfort", scheduled)

    async def async_set_fan(self, fan_mode: str) -> None:
        """Run the fan continuously, or hand it back to the system.

        ecobee has no standalone fan setting — continuous fan is a property of a
        hold, so this reapplies the current setpoints with the new fan mode.
        """
        if fan_mode not in ("auto", "on"):
            raise Exception(f"Unknown fan mode: {fan_mode}")
        data = await self._refreshed()
        heat, cool = setpoints_of(data)
        await self._client.set_hold(
            self.identifier,
            heat_f10=c_to_f10(heat) if heat is not None else None,
            cool_f10=c_to_f10(cool) if cool is not None else None,
            fan=fan_mode,
            hold_type=DEFAULT_HOLD_TYPE,
        )
        await self._persist_tokens()
        await self._safe_set("ecobee_fan_mode", fan_mode)

    async def async_current_comfort(self) -> str:
        """The live comfort profile ref — used by the flow condition card."""
        data = await self._refreshed()
        return comfort_profile_of(data)

    # ── Capability listeners ─────────────────────────────────────────────────
    async def _on_set_target(self, value: float, **kwargs) -> None:
        try:
            await self.async_set_target(float(value))
        except EcobeeError as e:
            self.error("set target failed:", e)
            raise Exception("Could not set the temperature on the thermostat.") from e

    async def _on_set_mode(self, value: str, **kwargs) -> None:
        try:
            await self.async_set_mode(value)
        except EcobeeError as e:
            self.error("set mode failed:", e)
            raise Exception("Could not change the thermostat mode.") from e

    async def _on_set_comfort(self, value: str, **kwargs) -> None:
        try:
            await self.async_set_comfort(value)
        except EcobeeError as e:
            self.error("set comfort profile failed:", e)
            raise Exception("Could not change the comfort profile.") from e

    async def _on_set_fan(self, value: str, **kwargs) -> None:
        try:
            await self.async_set_fan(value)
        except EcobeeError as e:
            self.error("set fan failed:", e)
            raise Exception("Could not change the fan mode.") from e

    # ── Helpers ──────────────────────────────────────────────────────────────
    async def _safe_set(self, cap: str, value: Any) -> None:
        if not self.has_capability(cap):
            return
        try:
            if self.get_capability_value(cap) != value:
                await self.set_capability_value(cap, value)
        except Exception as e:  # noqa: BLE001
            self.error(f"set {cap} failed:", e)

    async def _fire_triggers(self, comfort: str) -> None:
        # Indoor-temperature and mode triggers are auto-generated by Homey from
        # the measure_temperature / thermostat_mode system capabilities, so only
        # the custom comfort-profile trigger is fired here.
        if comfort and comfort != self._last_comfort:
            if self._last_comfort is not None:
                await self._trigger("comfort_changed", {"profile": comfort})
            self._last_comfort = comfort

    async def _trigger(self, card_id: str, tokens: dict) -> None:
        try:
            await self.homey.flow.get_device_trigger_card(card_id).trigger(self, tokens)
        except Exception as e:  # noqa: BLE001
            self.error(f"trigger {card_id} failed:", e)


homey_export = EcobeeThermostatDevice
