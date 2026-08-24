"""
Auth router — GitHub OAuth flow + API key management.

Endpoints:
  GET  /auth/github          Redirect to GitHub OAuth consent screen
  GET  /auth/callback        Handle GitHub code, upsert user, issue a short-lived exchange code
  POST /auth/token-exchange  Trade a callback exchange code for the real JWT
  POST /auth/logout          Client-side logout (token invalidation is stateless)
  GET  /auth/me              Return current user profile
  PUT  /auth/me/api-key      Store/update encrypted Anthropic API key
  DELETE /auth/me/api-key    Remove stored API key
"""
from __future__ import annotations

import os
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from web.backend.auth import create_access_token, exchange_github_code, get_current_user_id
from web.backend.crypto import decrypt_key, encrypt_key
from web.backend.db import get_session
from web.backend.models_db import AuditEvent, User
from web.backend.rate_limit import limiter

router = APIRouter(prefix="/auth", tags=["auth"])

# --- OAuth CSRF state -------------------------------------------------------
# Double-submit cookie: the state minted in /github is stashed in a
# short-lived httponly cookie and echoed back by GitHub as a query param on
# /callback. There's no existing session store in this app, and the state
# only needs to survive one browser redirect round trip, so a cookie is
# sufficient — no new persistent table.
_OAUTH_STATE_COOKIE = "oauth_state"
_OAUTH_STATE_MAX_AGE = 600  # seconds — generous for a login redirect round trip

# --- JWT exchange codes (Fix 4) ---------------------------------------------
# Handing the frontend a raw JWT via `?token=` puts it in browser history,
# Referer headers to any third party the landing page loads, and proxy/access
# logs. Instead /callback mints a short-lived, single-use opaque code and the
# frontend POSTs it here to get the real JWT back in a response body.
#
# In-memory + single-process: this app is started as a single uvicorn process
# (see web/backend/app.py's docstring) with no session store already wired up,
# and the code only needs to live for the few hundred ms between the redirect
# landing and the frontend's POST. If this backend ever runs with multiple
# API worker processes, move this dict to Redis (REDIS_URL is already used by
# Celery) so a code minted by one worker is visible to whichever worker
# handles the exchange.
_EXCHANGE_CODE_TTL = 60  # seconds
_exchange_codes: dict[str, tuple[str, float]] = {}


def _is_secure_cookie() -> bool:
    return os.environ.get("ENV") == "production"


def _store_exchange_code(token: str) -> str:
    # Opportunistically drop expired entries so this dict can't grow unbounded.
    now = time.time()
    for k in [k for k, (_, exp) in _exchange_codes.items() if exp <= now]:
        _exchange_codes.pop(k, None)
    code = secrets.token_urlsafe(32)
    _exchange_codes[code] = (token, now + _EXCHANGE_CODE_TTL)
    return code


@router.get("/github")
@limiter.limit("10/minute")
def github_login(request: Request):
    """Redirect browser to GitHub OAuth consent screen."""
    client_id = os.environ.get("GITHUB_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured")
    scope = "read:user,user:email"
    state = secrets.token_urlsafe(32)
    redirect = RedirectResponse(
        f"https://github.com/login/oauth/authorize?client_id={client_id}&scope={scope}&state={state}"
    )
    redirect.set_cookie(
        _OAUTH_STATE_COOKIE,
        state,
        max_age=_OAUTH_STATE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_is_secure_cookie(),
    )
    return redirect


@router.get("/callback")
async def github_callback(
    request: Request,
    session: Session = Depends(get_session),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Handle GitHub callback, upsert user, issue a short-lived exchange code, redirect to frontend."""
    from loguru import logger
    # Deliberately not logging request.url / query_params — GitHub's
    # authorization `code` is a sensitive, single-use credential and
    # shouldn't end up in logs.
    logger.info("OAuth callback received")

    frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:5173")

    # GitHub sends ?error= instead of ?code= when something goes wrong
    if error:
        msg = error_description or error
        resp = RedirectResponse(f"{frontend_url}/login?error={msg}")
        resp.delete_cookie(_OAUTH_STATE_COOKIE)
        return resp

    # CSRF check: the state GitHub echoes back must match the one minted (and
    # stashed in an httponly cookie) by /github before the redirect.
    cookie_state = request.cookies.get(_OAUTH_STATE_COOKIE)
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise HTTPException(status_code=400, detail="Invalid or missing OAuth state")

    if not code:
        raise HTTPException(status_code=400, detail="No OAuth code received from GitHub")

    github_user = await exchange_github_code(code)

    user = session.exec(
        select(User).where(User.github_id == github_user["github_id"])
    ).first()

    if not user:
        user = User(
            github_id=github_user["github_id"],
            github_login=github_user["github_login"],
            email=github_user.get("email"),
        )
        session.add(user)
        session.commit()
        session.refresh(user)

    token = create_access_token(user.id)
    exchange_code = _store_exchange_code(token)

    # Use /oauth (not /auth/callback) so Vite's /auth proxy rule doesn't intercept it
    resp = RedirectResponse(f"{frontend_url}/oauth?code={exchange_code}")
    resp.delete_cookie(_OAUTH_STATE_COOKIE)
    return resp


class TokenExchangeRequest(BaseModel):
    code: str


@router.post("/token-exchange")
def exchange_token(body: TokenExchangeRequest):
    """Trade a short-lived, single-use OAuth exchange code (see /callback) for the real JWT."""
    entry = _exchange_codes.pop(body.code, None)
    if not entry:
        raise HTTPException(status_code=400, detail="Invalid or already-used exchange code")
    token, expires_at = entry
    if time.time() > expires_at:
        raise HTTPException(status_code=400, detail="Exchange code expired")
    return {"access_token": token, "token_type": "bearer"}


@router.post("/logout")
def logout():
    """Stateless logout — client should discard the JWT."""
    return {"message": "Logged out"}


@router.get("/me")
def get_me(
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id": user.id,
        "github_login": user.github_login,
        "email": user.email,
        "has_api_key": user.anthropic_key_enc is not None,
        "has_gemini_key": user.gemini_key_enc is not None,
        "created_at": user.created_at,
    }


class ApiKeyUpdate(BaseModel):
    anthropic_api_key: str


@router.put("/me/api-key", status_code=204)
def update_api_key(
    body: ApiKeyUpdate,
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    """Store the user's Anthropic API key (Fernet-encrypted)."""
    if not body.anthropic_api_key.startswith("sk-ant-"):
        raise HTTPException(status_code=400, detail="Doesn't look like a valid Anthropic API key")

    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.anthropic_key_enc = encrypt_key(body.anthropic_api_key)
    session.add(user)

    session.add(AuditEvent(
        user_id=user_id,
        event_type="api_key_updated",
        ip_addr=request.client.host if request.client else None,
    ))
    session.commit()


@router.delete("/me/api-key", status_code=204)
def delete_api_key(
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    """Remove stored API key — user will need to re-enter before next run."""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.anthropic_key_enc = None
    session.add(user)
    session.commit()


class GeminiKeyUpdate(BaseModel):
    gemini_api_key: str


@router.put("/me/gemini-key", status_code=204)
def update_gemini_key(
    body: GeminiKeyUpdate,
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    """Store the user's Google Gemini API key (Fernet-encrypted)."""
    if not body.gemini_api_key.startswith("AI"):
        raise HTTPException(status_code=400, detail="Doesn't look like a valid Gemini API key (expected prefix 'AI')")

    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.gemini_key_enc = encrypt_key(body.gemini_api_key)
    session.add(user)

    session.add(AuditEvent(
        user_id=user_id,
        event_type="gemini_key_updated",
        ip_addr=request.client.host if request.client else None,
    ))
    session.commit()


@router.delete("/me/gemini-key", status_code=204)
def delete_gemini_key(
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    """Remove stored Gemini API key."""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.gemini_key_enc = None
    session.add(user)
    session.commit()
