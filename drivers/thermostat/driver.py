"""ecobee driver — pairing (ecobee sign-in, incl. MFA) and flow cards."""

from __future__ import annotations

import os
import sys
from typing import Any

# Make the app-root `ecobee` package importable from this nested driver module.
# NOTE: it is named `ecobee`, not `lib`, because the Homey runner already has its
# own top-level `lib` package in sys.modules — `from lib.x import` would resolve
# to the runner's and fail.
_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from homey.driver import Driver, ListDeviceProperties  # noqa: E402

from ecobee.api import EcobeeClient, identifier_of, label_of  # noqa: E402
from ecobee.auth import (  # noqa: E402
    EcobeeAuth,
    EcobeeAuthError,
    EcobeeError,
    InvalidCredentials,
    MFACodeExpired,
    MFACodeInvalid,
    MFACodeRequired,
    new_session,
)
from ecobee.const import DEFAULT_HOLD_TYPE, HOLD_TYPES  # noqa: E402

MFA_PROMPTS = {
    "otp": "Enter the 6-digit code from your authenticator app.",
    "sms": "Enter the code ecobee just texted you.",
    "email": "Enter the code ecobee just emailed you.",
    "recovery": "Enter one of your ecobee recovery codes.",
}


class EcobeeThermostatDriver(Driver):
    async def on_init(self) -> None:
        self._register_flow()
        self.log("ecobee driver initialized")

    # ── Flow cards ─────────────────────────────────────────────────────────
    def _register_flow(self) -> None:
        flow = self.homey.flow

        def hold_args(args) -> dict:
            """Pull the optional hold-duration args off a flow card."""
            hold_type = args.get("hold_type") or DEFAULT_HOLD_TYPE
            if hold_type not in HOLD_TYPES:
                hold_type = DEFAULT_HOLD_TYPE
            hours = args.get("hours")
            return {
                "hold_type": hold_type,
                "hold_hours": int(hours) if hours is not None else None,
            }

        async def set_comfort_run(args, **kwargs):
            await args["device"].async_set_comfort(args["profile"], **hold_args(args))

        async def set_comfort_named_run(args, **kwargs):
            await args["device"].async_set_comfort_by_name(args["name"], **hold_args(args))

        async def resume_run(args, **kwargs):
            await args["device"].async_resume_schedule()

        async def set_fan_run(args, **kwargs):
            await args["device"].async_set_fan(args["mode"])

        async def comfort_is_run(args, **kwargs) -> bool:
            current = await args["device"].async_current_comfort()
            return current.strip().lower() == str(args["profile"]).strip().lower()

        flow.get_action_card("set_comfort_profile").register_run_listener(set_comfort_run)
        flow.get_action_card("set_comfort_profile_named").register_run_listener(
            set_comfort_named_run
        )
        flow.get_action_card("resume_schedule").register_run_listener(resume_run)
        flow.get_action_card("set_fan_mode").register_run_listener(set_fan_run)
        flow.get_condition_card("comfort_profile_is").register_run_listener(comfort_is_run)

    # ── Pairing ────────────────────────────────────────────────────────────
    async def on_pair(self, session) -> None:
        # Scratch state lives on the DRIVER, not in this function's closure: if
        # Homey starts a fresh PairSession when the view re-inits, on_pair runs
        # again and a closure-local dict would silently reset — which surfaces as
        # an empty device list.
        self._pair_seq = getattr(self, "_pair_seq", 0) + 1
        sid = self._pair_seq

        state: dict[str, Any] = getattr(self, "_pair_state", None) or {
            "http": None,        # the sign-in session; MFA resume needs its cookies
            "mfa": None,         # pending MFACodeRequired context
            "auth": {},          # EcobeeAuth.to_dict()
            "thermostats": [],   # raw thermostat dicts
        }
        self._pair_state = state
        self.log(
            f"[pair {sid}] session started "
            f"(carrying {len(state['thermostats'])} thermostat(s))"
        )

        async def close_http() -> None:
            http = state.get("http")
            if http is not None and not http.closed:
                await http.close()
            state["http"] = None

        async def finish(auth: EcobeeAuth) -> dict:
            """Fetch the account's thermostats once sign-in has succeeded."""
            client = EcobeeClient(auth, state["http"])
            thermostats = await client.get_thermostats()
            if not thermostats:
                raise Exception("No thermostats were found on this ecobee account.")
            state.update(auth=auth.to_dict(), thermostats=thermostats, mfa=None)
            self.log(f"[pair {sid}] signed in -> {len(thermostats)} thermostat(s)")
            return {"status": "ok", "count": len(thermostats)}

        async def handle_login(data: dict) -> dict:
            """Sign in with the ecobee account, or ask for an MFA code."""
            email = ((data or {}).get("email") or "").strip()
            password = (data or {}).get("password") or ""
            if not email or not password:
                raise Exception("Enter your ecobee email and password.")

            await close_http()
            state["http"] = new_session()
            try:
                auth = await EcobeeAuth.login(state["http"], email, password)
            except MFACodeRequired as e:
                # Hold the session open — its cookie jar is what lets the code
                # submission below resume this same Auth0 login.
                state["mfa"] = {
                    "prompt_url": e.prompt_url,
                    "state": e.state,
                    "verifier": e.verifier,
                    "email": e.email,
                }
                self.log(f"[pair {sid}] MFA required ({e.challenge_type})")
                return {
                    "status": "mfa",
                    "challenge_type": e.challenge_type,
                    "message": MFA_PROMPTS.get(e.challenge_type, "Enter your verification code."),
                }
            except InvalidCredentials as e:
                await close_http()
                self.error("ecobee sign-in rejected:", e)
                raise Exception(f"Sign-in failed — {e}") from e
            except EcobeeAuthError as e:
                await close_http()
                self.error("ecobee sign-in failed:", e)
                raise Exception(f"Could not sign in to ecobee. {e}") from e
            except EcobeeError as e:
                await close_http()
                self.error("ecobee API error during pairing:", e)
                raise Exception("Could not reach the ecobee service. Try again.") from e
            return await finish(auth)

        async def handle_submit_mfa(data: dict) -> dict:
            """Finish a sign-in that Auth0 interrupted for a verification code."""
            code = ((data or {}).get("code") or "").strip()
            mfa = state.get("mfa")
            if not mfa or not state.get("http"):
                raise Exception("That sign-in attempt has ended — start again.")
            if not code:
                raise Exception("Enter the verification code.")
            try:
                auth = await EcobeeAuth.resume_with_mfa_code(
                    state["http"],
                    prompt_url=mfa["prompt_url"],
                    state=mfa["state"],
                    code=code,
                    verifier=mfa["verifier"],
                    email=mfa["email"],
                )
            except MFACodeInvalid as e:
                # Keep the session and prompt alive so the user can retype.
                raise Exception("That code was not accepted — check it and try again.") from e
            except MFACodeExpired as e:
                state["mfa"] = None
                await close_http()
                raise Exception("That code expired. Enter your email and password again.") from e
            except MFACodeRequired as e:
                state["mfa"] = {
                    "prompt_url": e.prompt_url,
                    "state": e.state,
                    "verifier": e.verifier,
                    "email": e.email,
                }
                return {
                    "status": "mfa",
                    "challenge_type": e.challenge_type,
                    "message": MFA_PROMPTS.get(e.challenge_type, "Enter your verification code."),
                }
            except EcobeeError as e:
                state["mfa"] = None
                await close_http()
                self.error("ecobee MFA step failed:", e)
                raise Exception(f"Could not finish signing in. {e}") from e
            return await finish(auth)

        async def handle_list_devices(_data: Any = None) -> list[ListDeviceProperties]:
            auth = state.get("auth") or {}
            out: list[ListDeviceProperties] = [
                {
                    "name": label_of(thermostat),
                    "data": {"id": identifier_of(thermostat)},
                    "store": {
                        "identifier": identifier_of(thermostat),
                        "email": auth.get("email"),
                        "refresh_token": auth.get("refresh_token"),
                    },
                }
                for thermostat in state["thermostats"]
            ]
            self.log(f"[pair {sid}] list_devices -> {[o['name'] for o in out]}")
            # The sign-in session has done its job; the device opens its own.
            await close_http()
            return out

        session.set_handler("login", handle_login)
        session.set_handler("submit_mfa", handle_submit_mfa)
        session.set_handler("list_devices", handle_list_devices)


homey_export = EcobeeThermostatDriver
