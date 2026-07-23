"""ecobee sign-in: Auth0 authorization-code + PKCE against ecobee's own tenant.

The flow, end to end:

    GET  /authorize                 -> 302 /u/login/identifier?state=…
    POST /u/login/identifier        -> 302 /u/login/password?state=…
    POST /u/login/password          -> 302 /authorize/resume?state=…
    GET  /authorize/resume          -> 302 to the next /u/* prompt, or to
                                       …/authCallback?code=…
    POST /oauth/token               -> access_token + refresh_token

Everything between the password step and the callback is Auth0's business: MFA
challenges, terms-of-service prompts, profile completion. Prompts whose primary
button just needs a click are auto-continued; prompts that need the user to
*type* something (a 6-digit code) raise `MFACodeRequired`, which the pairing
view catches so it can ask for the code and resume.

The whole login chain is cookie-bound, so it must run on one aiohttp session —
including the resume-after-MFA call, which happens in a later pair handler.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import ssl
import time
import urllib.parse

import aiohttp

from .const import (
    AUDIENCE,
    AUTH0_DOMAIN,
    AUTHORIZE_URL,
    IDENTIFIER_URL,
    PASSWORD_URL,
    REDIRECT_URI,
    RESUME_URL,
    SCOPES,
    TOKEN_REFRESH_MARGIN,
    TOKEN_URL,
    USER_AGENT_WEB,
    WEB_CLIENT_ID,
)

_LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# TLS / session
# ─────────────────────────────────────────────

def make_ssl_context() -> ssl.SSLContext:
    """SSL context backed by certifi's CA bundle.

    Homey's Python runtime ships no system CA store, so aiohttp's default
    context fails every HTTPS call with:
        CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate
    Always build sessions via `new_session()`.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - fall back to the system store if present
        return ssl.create_default_context()


def new_session() -> aiohttp.ClientSession:
    """An aiohttp session that verifies TLS on Homey and keeps cookies.

    The cookie jar matters: Auth0's universal-login chain carries its session in
    cookies, so every step of one sign-in must share a session.
    """
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=make_ssl_context()),
        cookie_jar=aiohttp.CookieJar(),
    )


# ─────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────

class EcobeeError(Exception):
    """Base ecobee error."""


class EcobeeAuthError(EcobeeError):
    """Authentication / token error."""


class InvalidCredentials(EcobeeAuthError):
    """The email or password was rejected at the login prompt."""


class InvalidGrant(EcobeeAuthError):
    """The refresh token was invalidated server-side — sign in again."""


class MFACodeRequired(EcobeeAuthError):
    """Auth0 wants a code typed in before the sign-in can finish.

    Carries what `resume_with_mfa_code` needs to pick the flow back up: the
    prompt URL to POST the code to, the prompt's state token, the PKCE verifier
    from the original /authorize call, and which kind of factor was chosen.
    """

    def __init__(
        self,
        *,
        prompt_url: str,
        state: str,
        challenge_type: str,
        verifier: str = "",
        email: str = "",
    ) -> None:
        super().__init__(f"MFA code required ({challenge_type})")
        self.prompt_url = prompt_url
        self.state = state
        self.challenge_type = challenge_type
        self.verifier = verifier
        self.email = email


class MFACodeInvalid(EcobeeAuthError):
    """The submitted MFA code was wrong — ask again without restarting."""


class MFACodeExpired(EcobeeAuthError):
    """The MFA prompt timed out — restart from email/password."""


# ─────────────────────────────────────────────
# PKCE helpers
# ─────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636 S256."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _html_headers(referer: str | None = None) -> dict:
    headers = {"User-Agent": USER_AGENT_WEB, "Accept": "text/html,*/*"}
    if referer:
        headers["Referer"] = referer
    return headers


def _form_headers(referer: str) -> dict:
    return {
        "User-Agent": USER_AGENT_WEB,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html,*/*",
        "Origin": f"https://{AUTH0_DOMAIN}",
        "Referer": referer,
    }


def _state_of(location: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get(
        "state", [""]
    )[0]


# ─────────────────────────────────────────────
# Login steps
# ─────────────────────────────────────────────

async def _authorize(session: aiohttp.ClientSession, challenge: str) -> str:
    """GET /authorize; return the state token bound to this login attempt."""
    params = {
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "audience": AUDIENCE,
        "state": _b64url(secrets.token_bytes(32)),
        "client_id": WEB_CLIENT_ID,
        "prompt": "login",
    }
    async with session.get(
        AUTHORIZE_URL, params=params, headers=_html_headers(), allow_redirects=False
    ) as r:
        if r.status not in (302, 303):
            body = (await r.text())[:200]
            raise EcobeeAuthError(f"authorize: expected a redirect, got {r.status}: {body!r}")
        loc = r.headers.get("Location", "")
    state = _state_of(loc)
    if not state:
        raise EcobeeAuthError(f"authorize: no state in redirect {loc!r}")
    return state


async def _post_prompt(
    session: aiohttp.ClientSession, url: str, state: str, form: dict
) -> str:
    """POST a /u/* prompt form; return the redirect Location on success."""
    async with session.post(
        url,
        params={"state": state},
        data=urllib.parse.urlencode(form),
        headers=_form_headers(f"{url}?state={state}"),
        allow_redirects=False,
    ) as r:
        if r.status in (302, 303):
            return r.headers.get("Location", "")
        text = await r.text()
    # Auth0 renders field errors as data-error-code="…" on a 200/400 re-render.
    m = re.search(r'data-error-code="([^"]+)"', text)
    code = m.group(1) if m else ""
    if code and any(
        s in code.lower() for s in ("password", "credential", "user", "lock", "blocked")
    ):
        raise InvalidCredentials(f"sign-in rejected ({code})")
    raise EcobeeAuthError(
        f"prompt {url} returned {r.status}"
        + (f" ({code})" if code else f": {text[:200]!r}")
    )


async def _identifier_step(
    session: aiohttp.ClientSession, state: str, email: str
) -> str:
    """Submit the email; advance to the password prompt."""
    loc = await _post_prompt(
        session,
        IDENTIFIER_URL,
        state,
        {
            "state": state,
            "username": email,
            "js-available": "true",
            "webauthn-available": "true",
            "is-brave": "false",
            "webauthn-platform-available": "true",
            "action": "default",
        },
    )
    if not urllib.parse.urlparse(loc).path.endswith("/u/login/password"):
        # Auth0 loops back to the identifier prompt for unknown emails.
        raise InvalidCredentials("that email is not recognised by ecobee")
    return _state_of(loc)


async def _password_step(
    session: aiohttp.ClientSession, state: str, email: str, password: str
) -> str:
    """Submit the password; advance to /authorize/resume."""
    loc = await _post_prompt(
        session,
        PASSWORD_URL,
        state,
        {"state": state, "username": email, "password": password, "action": "default"},
    )
    if not urllib.parse.urlparse(loc).path.endswith("/authorize/resume"):
        raise InvalidCredentials("that password was not accepted")
    return _state_of(loc)


async def _resume_to_code(session: aiohttp.ClientSession, resume_state: str) -> str:
    """Follow /authorize/resume until it hands back an authorization code.

    Auth0 can interpose several prompt pages (MFA, terms of service, profile
    completion) between the password step and the callback. Each loop handles
    one; the bound stops a redirect storm from spinning forever.
    """
    for _ in range(6):
        async with session.get(
            RESUME_URL,
            params={"state": resume_state},
            headers=_html_headers(),
            allow_redirects=False,
        ) as r:
            if r.status not in (302, 303):
                body = (await r.text())[:200]
                raise EcobeeAuthError(f"resume: expected a redirect, got {r.status}: {body!r}")
            loc = r.headers.get("Location", "")

        if loc.startswith(REDIRECT_URI):
            code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get(
                "code", [""]
            )[0]
            if not code:
                raise EcobeeAuthError(f"resume: callback carried no code ({loc!r})")
            return code

        if "/u/" in loc:
            resume_state = await _handle_prompt(session, loc)
            continue

        raise EcobeeAuthError(f"resume: unexpected redirect {loc!r}")

    raise EcobeeAuthError(
        "ecobee kept asking for more sign-in steps. Sign in at ecobee.com in a "
        "browser, clear whatever it asks for there, then pair again."
    )


async def _handle_prompt(session: aiohttp.ClientSession, loc: str) -> str:
    """Advance one Auth0 prompt page; return the next resume state.

    These pages are React-rendered, so there is no parseable <form>. Auth0's
    convention saves us: the POST target is the same path we landed on, and
    `state=<state>&action=default` is the primary button for prompts that only
    need an acknowledgement. Prompts that need a typed code are recognised by
    path and bounced back to the caller instead.
    """
    url = loc if loc.startswith("http") else f"https://{AUTH0_DOMAIN}{loc}"
    parsed = urllib.parse.urlparse(url)
    state = urllib.parse.parse_qs(parsed.query).get("state", [""])[0]
    if not state:
        raise EcobeeAuthError(f"prompt: no state in {url!r}")

    if re.search(r"/u/mfa-(otp|sms|email|recovery-code)(-challenge)?(/|\?|$)", parsed.path):
        if "otp" in parsed.path:
            kind = "otp"
        elif "sms" in parsed.path:
            kind = "sms"
        elif "email" in parsed.path:
            kind = "email"
        else:
            kind = "recovery"
        raise MFACodeRequired(prompt_url=url, state=state, challenge_type=kind)

    async with session.post(
        url,
        data={"state": state, "action": "default"},
        headers=_form_headers(url),
        allow_redirects=False,
    ) as r:
        if r.status not in (302, 303):
            body = (await r.text())[:200]
            raise EcobeeAuthError(
                "ecobee is showing a sign-in step this app cannot complete "
                f"({parsed.path}). Sign in at ecobee.com in a browser, finish "
                f"whatever it asks for, then pair again. [{r.status}: {body!r}]"
            )
        new_loc = r.headers.get("Location", "")

    next_state = _state_of(new_loc)
    if not next_state:
        raise EcobeeAuthError(f"prompt: no state in follow-up {new_loc!r}")
    return next_state


async def _exchange_code(
    session: aiohttp.ClientSession, code: str, verifier: str
) -> dict:
    """Trade the authorization code for tokens."""
    return await _token_request(
        session,
        {
            "grant_type": "authorization_code",
            "client_id": WEB_CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        },
    )


async def _token_request(session: aiohttp.ClientSession, body: dict) -> dict:
    try:
        async with session.post(
            TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        ) as r:
            text = await r.text()
            status = r.status
    except aiohttp.ClientError as e:
        raise EcobeeAuthError(f"token request network error: {e}") from e
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {"raw": text[:200]}
    if status == 200 and payload.get("access_token"):
        return payload
    if status == 400 and payload.get("error") == "invalid_grant":
        raise InvalidGrant(payload.get("error_description") or "refresh token rejected")
    raise EcobeeAuthError(f"token request failed ({status}): {payload}")


# ─────────────────────────────────────────────
# The auth handle
# ─────────────────────────────────────────────

class EcobeeAuth:
    """Holds the refresh token and mints access tokens from it.

    Built either by `login()` during pairing or by `from_dict()` on device init.
    `tokens_rotated` flags that Auth0 handed back a new refresh token, so the
    device layer knows to write it back to the store.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str,
        *,
        email: str = "",
    ) -> None:
        self._session = session
        self.refresh_token = refresh_token
        self.email = email
        self.access_token: str | None = None
        self._expiry: float = 0.0
        self.tokens_rotated = False

    # ── Sign-in ────────────────────────────────────────────────

    @classmethod
    async def login(
        cls, session: aiohttp.ClientSession, email: str, password: str
    ) -> "EcobeeAuth":
        """Run the full universal-login chain.

        Raises `MFACodeRequired` (carrying the PKCE verifier and email) when
        Auth0 needs a code; the caller should collect it and call
        `resume_with_mfa_code` on this same session.
        """
        verifier, challenge = _make_pkce()
        state = await _authorize(session, challenge)
        pw_state = await _identifier_step(session, state, email)
        resume_state = await _password_step(session, pw_state, email, password)
        try:
            code = await _resume_to_code(session, resume_state)
        except MFACodeRequired as e:
            e.verifier = verifier
            e.email = email
            raise
        return cls._from_tokens(
            session, await _exchange_code(session, code, verifier), email
        )

    @classmethod
    async def resume_with_mfa_code(
        cls,
        session: aiohttp.ClientSession,
        *,
        prompt_url: str,
        state: str,
        code: str,
        verifier: str,
        email: str,
    ) -> "EcobeeAuth":
        """Submit the MFA code and finish the sign-in.

        `session` must be the one `login()` ran on — its cookie jar holds the
        Auth0 session this prompt belongs to.
        """
        async with session.post(
            prompt_url,
            data={"state": state, "code": code},
            headers=_form_headers(prompt_url),
            allow_redirects=False,
        ) as r:
            if r.status == 200:
                # Auth0 re-renders the prompt on a bad or stale code.
                page = (await r.text()).lower()
                if "expired" in page:
                    raise MFACodeExpired("that code has expired — sign in again")
                raise MFACodeInvalid("that code was not accepted")
            if r.status not in (302, 303):
                body = (await r.text())[:200]
                raise EcobeeAuthError(f"MFA submit returned {r.status}: {body!r}")
            loc = r.headers.get("Location", "")

        if loc.startswith(REDIRECT_URI):
            auth_code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get(
                "code", [""]
            )[0]
            if not auth_code:
                raise EcobeeAuthError(f"MFA callback carried no code ({loc!r})")
        else:
            next_state = _state_of(loc)
            if not next_state:
                raise EcobeeAuthError(f"MFA submit: unexpected redirect {loc!r}")
            try:
                auth_code = await _resume_to_code(session, next_state)
            except MFACodeRequired as e:
                # A second factor was chained; carry the context through again.
                e.verifier = verifier
                e.email = email
                raise

        return cls._from_tokens(
            session, await _exchange_code(session, auth_code, verifier), email
        )

    @classmethod
    def _from_tokens(
        cls, session: aiohttp.ClientSession, tokens: dict, email: str
    ) -> "EcobeeAuth":
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise EcobeeAuthError("ecobee did not return a refresh token")
        auth = cls(session, refresh_token, email=email)
        auth.access_token = tokens["access_token"]
        auth._expiry = time.time() + int(tokens.get("expires_in") or 3600)
        return auth

    # ── Lifecycle ──────────────────────────────────────────────

    async def ensure_valid(self) -> str:
        """Return a usable access token, refreshing it first if needed."""
        if self.access_token and time.time() < self._expiry - TOKEN_REFRESH_MARGIN:
            return self.access_token
        await self.refresh()
        return self.access_token or ""

    async def refresh(self) -> None:
        if not self.refresh_token:
            raise InvalidGrant("no refresh token — pair the thermostat again")
        payload = await _token_request(
            self._session,
            {
                "grant_type": "refresh_token",
                "client_id": WEB_CLIENT_ID,
                "refresh_token": self.refresh_token,
            },
        )
        self.access_token = payload["access_token"]
        self._expiry = time.time() + int(payload.get("expires_in") or 3600)
        new_refresh = payload.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = new_refresh
            self.tokens_rotated = True
        _LOGGER.debug("ecobee token refreshed; expires in %ss", payload.get("expires_in"))

    # ── Serialization (device store) ───────────────────────────

    def to_dict(self) -> dict:
        return {"email": self.email, "refresh_token": self.refresh_token}

    @classmethod
    def from_dict(cls, d: dict, session: aiohttp.ClientSession) -> "EcobeeAuth":
        return cls(
            session,
            d.get("refresh_token") or "",
            email=d.get("email") or "",
        )
