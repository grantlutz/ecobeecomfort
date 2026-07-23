# Installation guide

This app is installed from source onto a Homey Pro with the Homey CLI. Expect
10–20 minutes the first time, most of it waiting on Docker.

## What you need

- **Homey Pro** on the same local network as your computer, running firmware
  **13.0.0 or newer** (the Python app runtime).
- **An ecobee account** with your thermostat(s) already set up and online in
  the ecobee app.
- On your computer:
  - **Node.js 18+** (for the Homey CLI)
  - **Docker Desktop** (the CLI cross-compiles the app's Python dependencies
    inside Linux containers; needed on every OS)
  - **Git**

## 1. Install the Homey CLI and sign in

```bash
npm install -g homey
homey login
```

`homey login` opens a browser to link your Athom account. Afterwards, confirm
the CLI can see your Homey:

```bash
homey app list
```

## 2. Get the source

```bash
git clone https://github.com/johndellarciprete/ecobeecomfort.git
cd ecobeecomfort
```

## 3. Build the Python dependencies

With Docker Desktop running:

```bash
homey app dependencies install
```

This pulls Athom's builder images and produces `python_packages/<arch>/.venv`
for both `arm64` and `amd64`. It only needs to be repeated when the
`pythonPackages` list in `app.json` changes.

> **Windows:** two known issues, both with easy fixes — see
> [Windows workarounds](#windows-workarounds) below before continuing.

## 4. Install onto your Homey

```bash
homey app validate     # optional sanity check
homey app install
```

`homey app install` puts the app on the Homey permanently (it survives
restarts). Use `homey app run` instead if you want a temporary session with
live logs — the app is removed when you press Ctrl-C.

## 5. Pair the thermostat

In the Homey mobile app:

1. **Devices → + → ecobee Comfort → ecobee Thermostat**
2. Enter your **ecobee email and password** (the same ones the ecobee app
   uses).
3. If your account has two-step verification, ecobee sends a code (SMS,
   authenticator, or email) — enter it in the field that appears.
4. Every thermostat on the account is listed. **Tap one to add it.** To add
   more, run *Add device* again — the pairing remembers your sign-in for the
   session.

Your password is used only during pairing and never stored; the device keeps a
refresh token.

## 6. Pair room sensors (optional)

After at least one thermostat is paired and has completed a poll (give it a
few minutes after pairing):

1. **Devices → + → ecobee Comfort → ecobee Room Sensor**
2. No sign-in — your sensors are listed immediately, named like
   *"Bedroom (Downstairs)"*, including each thermostat's built-in sensor.
3. Tap to add.

Sensor readings update when the thermostat polls (~5 minutes).

## 7. Try it

On the thermostat device, open the **Comfort Profile** picker and set **Away**:
the Hold sensor flips to *Holding* and the ecobee app shows an indefinite Away
hold. Set it back to **Schedule** and the hold clears.

---

## Windows workarounds

### `uv_cache … Permission denied` during dependencies install

`homey app dependencies install` can fail with:

```
x Failed to download `<package>`
  `-> failed to rename file from /uv_cache/... : Permission denied (os error 13)
```

The CLI bind-mounts the uv cache from NTFS into the Linux build container,
where renames fail. Workaround: run the same builder image yourself with the
cache kept inside the container, writing the venv where the CLI expects it.
From Git Bash, in the app directory:

```bash
PROJ=$(mktemp -d)
cat > "$PROJ/pyproject.toml" <<'EOF'
[project]
name = "com-dellarciprete-ecobee"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["aiohttp", "certifi"]
EOF
printf '3.13' > "$PROJ/.python-version"

APP=$(pwd -W 2>/dev/null || pwd)
for arch in arm64 amd64; do
  mkdir -p "python_packages/$arch/.venv"
  MSYS_NO_PATHCONV=1 docker run --rm --platform "linux/$arch" \
    -v "$APP/python_packages/$arch/.venv:/.venv" -v "$PROJ:/project" -w /project \
    -e UV_CACHE_DIR=/tmp/uvcache \
    "ghcr.io/athombv/python-homey-app-builder-$arch:latest" \
    uv sync --active --no-dev
done
printf '3.13' > python_packages/.python-version
```

### Dangling symlinks break packaging

The cross-compiled venvs contain Linux symlinks that don't exist on Windows,
and `homey app validate` / `install` throws while copying them. After **every**
dependencies install, run:

```bash
bash scripts/fix_windows_venv.sh
```

It deletes the (unshipped) interpreter symlinks; nothing else changes.

### `getaddrinfo ENOTFOUND …homey.homeylocal.com` during install

The CLI reaches your Homey via a public DNS name that resolves to its LAN IP
(e.g. `192-168-1-23.homey.homeylocal.com` → `192.168.1.23`). Routers or corporate
DNS with **rebind protection** refuse such answers. Fixes, pick one:

- Add a hosts-file entry (elevated PowerShell), replacing the IP with your
  Homey's:

  ```powershell
  Add-Content C:\Windows\System32\drivers\etc\hosts "`n192.168.1.23`t192-168-1-23.homey.homeylocal.com"
  ipconfig /flushdns
  ```

  Remove/update the line if your Homey's IP ever changes — consider a DHCP
  reservation for it.
- Or allowlist `homeylocal.com` in your router's DNS-rebind settings.
- Or disconnect from any corporate VPN and retry.

---

## Troubleshooting

**Sign-in fails immediately** — check the email/password by logging in at
ecobee.com. If ecobee is showing you a prompt there (new terms of service,
password reset, MFA enrollment), complete it in the browser first; the pairing
flow can only auto-acknowledge simple prompts.

**"That code was not accepted"** — verification codes are short-lived; request
a fresh sign-in and type the newest code promptly.

**Thermostat shows "Sign-in expired — please re-pair"** — the refresh token was
invalidated, usually after an ecobee password change. Remove and re-add the
device (room sensors keep working through any paired thermostat and don't need
re-pairing).

**Comfort Profile shows `Custom`** — the thermostat is holding something that
isn't one of the three stock profiles: either a bare temperature hold (e.g. you
nudged the dial) or a custom climate. That's a status, not an error; it clears
when you pick a profile or resume the schedule.

**Changes on the thermostat take a while to appear in Homey** — expected; the
app polls every 5 minutes (ecobee asks for ≥3-minute spacing). Commands from
Homey are pushed immediately.

**Room sensor stuck on "Waiting for the ecobee thermostat…"** — the sensor is
fed by its parent thermostat device. Make sure that thermostat is still paired
and available, then wait one poll cycle.

**Verify everything outside Homey** — the repo ships a self-test that checks
the parsing logic offline, and (optionally, with credentials) does a live
sign-in and prints every thermostat and sensor:

```bash
python -m ecobee._selftest
ECOBEE_EMAIL=you@example.com ECOBEE_PASSWORD=... python -m ecobee._selftest
```

Run the live variant in an interactive terminal — it prompts for a 2FA code.
On PowerShell, set the variables first:

```powershell
$env:ECOBEE_EMAIL="you@example.com"; $env:ECOBEE_PASSWORD="..."; python -m ecobee._selftest
```
