# ecobee Comfort — Homey app

Control your ecobee thermostat from Homey — **including its comfort profiles**
(Home / Away / Sleep, plus any custom profile you created in the ecobee app),
which the existing Homey ecobee apps don't expose.

Works with any account: no ecobee developer API key required (ecobee closed
developer registration — see [How sign-in works](#how-sign-in-works)).

## Features

**ecobee Thermostat**

| Capability | What it does |
|---|---|
| Comfort Profile | Picker: Schedule / Home / Away / Sleep. Picking a profile sets an indefinite hold; picking Schedule resumes the program. Shows `Custom` (read-only) when a setpoint hold or custom climate is running |
| Hold | Sensor: following schedule / holding / vacation |
| Mode | Off / Heat / Cool / Auto |
| Target temperature | Whole-degree dial. In Auto it shows and edits the *in-play* side of the heat/cool band (cooling on a summer afternoon, heating on a winter night), nudging the other side only if ecobee's minimum gap forces it |
| Fan | Auto / On (continuous) |
| Temperature & humidity | Indoor readings |

**ecobee Room Sensor**

Temperature and motion from every SmartSensor / room sensor, plus the
thermostat's built-in sensor (which also reports humidity). Sensors are read
through the paired thermostat, so they cost **zero extra API calls** and pair
with no sign-in. Readings update at the thermostat's poll cadence (~5 minutes)
— fine for presence logic, not for a burglar alarm.

**Flow cards**

- *Actions:* Set comfort profile (with "hold until" choice) · Set comfort
  profile by name (for custom profiles) · Resume the schedule · Set mode ·
  Set fan mode · Set temperature
- *Condition:* Comfort profile is / is not …
- *Triggers:* Comfort profile changed · Indoor temperature changed · Mode
  changed — plus Homey's built-in motion/temperature cards on the room sensors

Example flow: *"When everyone has left home → Set comfort profile to Away"* —
one card, and the thermostat picks up Away's setpoints and fan settings itself.

## How sign-in works

ecobee closed public developer-portal registration, so the documented
PIN-authorization API flow is unavailable to new users. This app instead signs
in the same way ecobee.com itself does: OAuth2 authorization-code + PKCE
against ecobee's Auth0 tenant, using your ordinary ecobee email and password.
Two-step verification (SMS / authenticator / email codes) is supported during
pairing.

- Only the **refresh token** is stored on your Homey. Your password is used
  once during pairing and never saved.
- This is not a published API. If ecobee reworks their login, sign-in may break
  until this app is updated (`ecobee/auth.py` is where it lives).
- If you change your ecobee password, the stored token is eventually
  invalidated — just re-pair.

## Installation

This app is installed from source with the Homey CLI — see
**[INSTALL.md](INSTALL.md)** for the full step-by-step guide, including the
Windows-specific workarounds.

The short version:

```bash
npm install -g homey
homey login
git clone https://github.com/johndellarciprete/ecobeecomfort.git
cd ecobeecomfort
homey app install
```

Then in the Homey app: **Add device → ecobee Comfort → ecobee Thermostat**,
sign in, add your thermostat(s) — and afterwards **ecobee Room Sensor** to add
any sensors.

## Notes & limits

- ecobee asks that a thermostat not be polled more than once every 3 minutes;
  this app polls every 5. Changes made on the thermostat itself can therefore
  take up to 5 minutes to show in Homey. Changes made *from* Homey are pushed
  immediately.
- Comfort-profile holds default to **indefinite** — the profile stays until you
  change it or resume the schedule. The flow cards can instead hold *until the
  next scheduled change*.
- Temperatures are stored in °C internally (Homey's convention) and displayed
  in your Homey's unit; ecobee's wire format (tenths of °F) is converted at the
  API boundary.

## Development

```bash
# offline checks — no Homey, no network, no credentials
python -m ecobee._selftest

# plus a real sign-in and a live dump of every thermostat and sensor
# (interactive terminal only — it prompts for a 2FA code if your account has it)
ECOBEE_EMAIL=you@example.com ECOBEE_PASSWORD=... python -m ecobee._selftest
```

Project layout:

```
ecobee/            auth (Auth0 PKCE flow), REST client, JSON readers, registry
drivers/thermostat  thermostat device, pairing (sign-in + MFA), flow cards
drivers/sensor      room sensors, fed from the thermostat's poll — no API calls
```

## Disclaimer

This is an unofficial, community-built app. It is not affiliated with,
endorsed by, or supported by ecobee or Generac. Use at your own risk; the
sign-in mechanism mirrors ecobee's own web app and may stop working if ecobee
changes it.
