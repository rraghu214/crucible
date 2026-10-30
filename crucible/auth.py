"""Bearer-token gates for the Crucible control plane.

Every gate here **fails closed**. If the token is not configured the endpoint
returns 503 and refuses to serve, rather than quietly serving everybody.

That choice is the whole point. An autonomous agent's control plane is a
different kind of surface from a chat endpoint: a subscription carries
`allowed_side_effects` and a `budget`, so whoever can write one can decide what
the agent is allowed to do and how much it may spend. An unauthenticated write
path there is not a missing feature, it is a way to hand an anonymous caller
the authority the whole design rests on.

`if expected and not compare_digest(...)` is the shape to avoid: it reads like a
check and behaves like an open door whenever the variable is unset, which is
exactly the state a fresh checkout is in.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import os

from fastapi import Header, HTTPException

CONTROL_TOKEN_ENV = "CRUCIBLE_CONTROL_TOKEN"
COMPLETION_TOKEN_ENV = "CRUCIBLE_COMPLETION_TOKEN"
SITE_USER_ENV = "CRUCIBLE_UI_USER"
SITE_PASSWORD_ENV = "CRUCIBLE_UI_PASSWORD"
#: Reachable without the site login, so health checks keep working.
SITE_LOGIN_EXEMPT = frozenset({"/healthz"})


def site_login_enabled() -> bool:
    """The site login is on only when both its variables are set.

    It exists for one deployment shape: the dashboard published beyond
    localhost (operator decision, 31 October 2026, until submission). Local use
    and the test suite set neither variable and are unaffected. Unlike the
    token gates it is opt-in, because the default deployment is loopback-only.
    """
    return bool(os.getenv(SITE_USER_ENV, "").strip() and os.getenv(SITE_PASSWORD_ENV, ""))


def site_login_half_configured() -> str:
    """The variable that is missing when exactly one of the pair is set, else "".

    Opt-in means a half-configured login leaves the site open; startup warns
    rather than staying silent (assertion 44.1, operator decision 31 October 2026).
    """
    user = bool(os.getenv(SITE_USER_ENV, "").strip())
    password = bool(os.getenv(SITE_PASSWORD_ENV, ""))
    if user == password:
        return ""
    return SITE_PASSWORD_ENV if user else SITE_USER_ENV


def site_login_ok(authorization: str | None) -> bool:
    """True for valid Basic credentials, or for a valid control/completion token.

    The dashboard sends ``Authorization: Bearer <control token>`` on writes, and a
    request carries one Authorization header, so the Basic credentials cannot
    ride along. A holder of either token already has more authority than the
    site login grants, so accepting it here widens nothing.
    """
    value = (authorization or "").strip()
    if value.startswith("Bearer "):
        supplied = value.removeprefix("Bearer ").strip()
        for env_name in (CONTROL_TOKEN_ENV, COMPLETION_TOKEN_ENV):
            expected = os.getenv(env_name, "").strip()
            if expected and hmac.compare_digest(supplied, expected):
                return True
        return False
    # Fails closed on its own: unconfigured, an empty user and password would
    # otherwise "match" the empty variables.
    if not value.startswith("Basic ") or not site_login_enabled():
        return False
    try:
        user, _, password = base64.b64decode(value.removeprefix("Basic ").strip(), validate=True) \
            .decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    # Both compared, always, so the response time does not say which one was wrong.
    user_ok = hmac.compare_digest(user.encode(), os.getenv(SITE_USER_ENV, "").strip().encode())
    password_ok = hmac.compare_digest(password.encode(), os.getenv(SITE_PASSWORD_ENV, "").encode())
    return user_ok and password_ok


def _require(env_name: str, authorization: str | None, *, surface: str) -> None:
    expected = os.getenv(env_name, "").strip()
    if not expected:
        raise HTTPException(
            503,
            f"{env_name} is not configured; the {surface} refuses to serve without it",
        )
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, f"invalid {surface} token")


async def require_control(authorization: str | None = Header(default=None)) -> None:
    """Guard anything that creates authority, starts work, or spends money."""
    _require(CONTROL_TOKEN_ENV, authorization, surface="control plane")


async def require_completion(authorization: str | None = Header(default=None)) -> None:
    """Guard the callback that resumes a parked node from outside this process.

    A separate token from the control plane: the holder is a remote job service,
    which should be able to complete work it was given without also being able
    to write subscriptions.
    """
    _require(COMPLETION_TOKEN_ENV, authorization, surface="completion callback")
