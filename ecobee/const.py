"""Constants for the ecobee integration.

ecobee closed public developer-portal API-key registration, so the classic
`response_type=ecobeePin` flow is only usable by accounts that already hold a
key. This app instead signs in through ecobee's own Auth0 tenant with the
authorization-code + PKCE universal-login flow — the same one ecobee.com uses,
which means MFA is handled by Auth0's hosted prompt pages rather than by us.
"""

DOMAIN = "ecobee"
MANUFACTURER = "ecobee"

# ─────────────────────────────────────────────────────────────
# Auth0 tenant (sign-in)
# ─────────────────────────────────────────────────────────────
AUTH0_DOMAIN = "auth.ecobee.com"

# /authorize is the universal-login entry point: GET it with the PKCE challenge
# and follow the 302 to /u/login/identifier.
AUTHORIZE_URL = f"https://{AUTH0_DOMAIN}/authorize"

# /authorize/resume is where Auth0 sends us back after each /u/* prompt step.
# It either chains to another prompt or redirects to REDIRECT_URI with the code.
RESUME_URL = f"https://{AUTH0_DOMAIN}/authorize/resume"

# The two login prompt endpoints; both take form-encoded POST bodies.
IDENTIFIER_URL = f"https://{AUTH0_DOMAIN}/u/login/identifier"
PASSWORD_URL = f"https://{AUTH0_DOMAIN}/u/login/password"

# /oauth/token: code -> tokens, refresh_token -> tokens.
TOKEN_URL = f"https://{AUTH0_DOMAIN}/oauth/token"

# ecobee's web-app callback. We never navigate to it — the authorization code is
# lifted straight out of the Location header of the final 302.
REDIRECT_URI = "https://www.ecobee.com/home/authCallback"

# ecobee's public web client. A public Auth0 client doing authorization-code +
# PKCE needs no client_secret.
WEB_CLIENT_ID = "183eORFPlXyz9BbDZwqexHPBQoVjgadh"

# What Auth0 audits the access token against — not the same as the REST base URL.
AUDIENCE = "https://prod.ecobee.com/api/v1"

# `offline_access` is what makes Auth0 issue a refresh token; without it the
# user would have to sign in again every hour.
SCOPES = "openid smartRead smartWrite piiRead piiWrite offline_access"

# Auth0's universal-login pages render differently for obviously-scripted
# clients, so present a normal browser UA throughout the sign-in chain.
USER_AGENT_WEB = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.0 Safari/605.1.15"
)

# Refresh the access token this many seconds before it actually expires.
TOKEN_REFRESH_MARGIN = 90

# ─────────────────────────────────────────────────────────────
# REST API
# ─────────────────────────────────────────────────────────────
API_BASE = "https://api.ecobee.com/1"
API_THERMOSTAT = f"{API_BASE}/thermostat"

# ecobee's docs ask for no more than one poll per thermostat every 3 minutes.
DEFAULT_POLL_INTERVAL = 300  # seconds
MIN_POLL_INTERVAL = 180

# ─────────────────────────────────────────────────────────────
# Comfort profiles ("climates" in ecobee's API)
# ─────────────────────────────────────────────────────────────
# The three climates every ecobee ships with. A thermostat may also carry custom
# climates; those are reachable through the "set comfort profile by name" flow
# card and are reported on the capability as `custom`.
CLIMATE_HOME = "home"
CLIMATE_AWAY = "away"
CLIMATE_SLEEP = "sleep"
STANDARD_CLIMATES = (CLIMATE_HOME, CLIMATE_AWAY, CLIMATE_SLEEP)

# Capability value meaning "no hold — follow the programmed schedule". Selecting
# it resumes the program instead of creating a hold.
COMFORT_SCHEDULE = "schedule"
# Reported (never selectable) when a custom climate or a bare setpoint hold is
# running, so the picker never lies about what the thermostat is doing.
COMFORT_CUSTOM = "custom"

# ecobee generates these two automatically from its occupancy sensors; they are
# holds, but they map cleanly onto the named climates for display.
AUTO_EVENT_CLIMATE = {
    "autoAway": CLIMATE_AWAY,
    "autoHome": CLIMATE_HOME,
}

# How long a comfort-profile hold lasts by default. `indefinite` keeps the
# profile until the program is resumed or another profile is set;
# `nextTransition` would drop it at the schedule's next scheduled change.
DEFAULT_HOLD_TYPE = "indefinite"
HOLD_TYPES = ("indefinite", "nextTransition", "holdHours")

# ─────────────────────────────────────────────────────────────
# Mode / fan / status maps  (API value  <->  Homey capability slug)
# ─────────────────────────────────────────────────────────────
MODE_API_TO_HOMEY = {
    "off": "off",
    "heat": "heat",
    "cool": "cool",
    "auto": "auto",
    # Aux heat only (e.g. a dual-fuel system whose aux is a gas or propane
    # furnace) gets its own picker value rather than passing for plain heat.
    "auxHeatOnly": "aux",
}
# `aux` is deliberately absent: setting it goes through the aux-heat-only path
# (`async_set_aux_heat_only`), which also remembers the mode it replaces.
MODE_HOMEY_TO_API = {
    "off": "off",
    "heat": "heat",
    "cool": "cool",
    "auto": "auto",
}
HOMEY_MODE_AUX = "aux"

# The thermostat_mode picker values — kept identical to the driver's
# capabilitiesOptions in app.json, and pushed onto devices paired before `aux`
# existed (see the thermostat device's on_init).
THERMOSTAT_MODE_VALUES = (
    {"id": "off", "title": {"en": "Off"}},
    {"id": "heat", "title": {"en": "Heat"}},
    {"id": "cool", "title": {"en": "Cool"}},
    {"id": "auto", "title": {"en": "Auto"}},
    {"id": HOMEY_MODE_AUX, "title": {"en": "Aux heat"}},
)

# The mode the thermostat was in before aux heat only is kept in the device
# store under this key so the restore card can put it back.
HVAC_MODE_AUX_HEAT_ONLY = "auxHeatOnly"
PREV_HVAC_MODE_STORE_KEY = "prev_hvac_mode"
# Restored when aux heat was switched on outside Homey and nothing was saved.
AUX_RESTORE_FALLBACK_MODE = "heat"

# ecobee's fan control is binary: run continuously, or follow the system.
FAN_MODES = ("auto", "on")

# equipmentStatus is a comma-separated list of whatever is energised right now.
# Order matters — the first match wins, so compressor states beat plain fan.
EQUIPMENT_TO_STATE = (
    ("compCool", "cooling"),
    ("coolCompressor", "cooling"),
    ("heatPump", "heating"),
    ("auxHeat", "heating"),
    ("compHotWater", "heating"),
    ("heat", "heating"),
    ("cool", "cooling"),
    ("fan", "fan"),
    ("ventilator", "fan"),
)

# Hold-status capability values.
HOLD_SCHEDULE = "schedule"
HOLD_HOLD = "hold"
HOLD_VACATION = "vacation"

# ecobee status-envelope codes that mean "your token is no longer good".
AUTH_ERROR_CODES = (14, 16)
