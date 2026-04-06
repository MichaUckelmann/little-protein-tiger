"""
Auth router — GitHub OAuth flow + API key management.

Endpoints:
  GET  /auth/github          Redirect to GitHub OAuth consent screen
  GET  /auth/callback        Handle GitHub code, upsert user, return JWT
  POST /auth/logout          Client-side logout (token invalidation is stateless)
  GET  /auth/me              Return current user profile
  PUT  /auth/me/api-key      Store/update encrypted Anthropic API key
  DELETE /auth/me/api-key    Remove stored API key
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from web.backend.auth import create_access_token, exchange_github_code, get_current_user_id
from web.backend.crypto import decrypt_key, encrypt_key
from web.backend.db import get_session
from web.backend.models_db import AuditEvent, User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/github")
def github_login():
    """Redirect browser to GitHub OAuth consent screen."""
    client_id = os.environ.get("GITHUB_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured")
    scope = "read:user,user:email"
    return RedirectResponse(
        f"https://github.com/login/oauth/authorize?client_id={client_id}&scope={scope}"
    )


@router.get("/callback")
async def github_callback(
    request: Request,
    session: Session = Depends(get_session),
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Handle GitHub callback, upsert user, issue JWT, redirect to frontend."""
    from loguru import logger
    logger.info(f"OAuth callback — full URL: {request.url}")
    logger.info(f"OAuth callback — query params: {dict(request.query_params)}")

    # GitHub sends ?error= instead of ?code= when something goes wrong
    if error:
        frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:5173")
        msg = error_description or error
        return RedirectResponse(f"{frontend_url}/login?error={msg}")
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
    frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:5173")
    # Use /oauth (not /auth/callback) so Vite's /auth proxy rule doesn't intercept it
    return RedirectResponse(f"{frontend_url}/oauth?token={token}")


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
