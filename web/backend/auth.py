"""
Authentication helpers:
  - GitHub OAuth code exchange → user dict
  - JWT access token creation + decoding
  - FastAPI dependency: get_current_user_id
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

_ALGORITHM = "HS256"
_ACCESS_EXPIRE_HOURS = 8

# auto_error=False so we can return 401 with a clearer message
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


def _jwt_secret() -> str:
    s = os.environ.get("JWT_SECRET", "")
    if not s:
        raise RuntimeError("JWT_SECRET env var not set")
    return s


def create_access_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(hours=_ACCESS_EXPIRE_HOURS)
    return jwt.encode({"sub": str(user_id), "exp": expire}, _jwt_secret(), algorithm=_ALGORITHM)


def _decode_token(token: str) -> int:
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[_ALGORITHM])
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user_id(token: Optional[str] = Depends(_oauth2_scheme)) -> int:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _decode_token(token)


async def exchange_github_code(code: str) -> dict:
    """
    Exchange a GitHub OAuth authorisation code for user profile data.
    Returns dict with github_id, github_login, email (may be None).
    """
    client_id = os.environ.get("GITHUB_CLIENT_ID", "")
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET env vars not set")

    async with httpx.AsyncClient(timeout=15) as client:
        # Step 1: exchange code for GitHub access token
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={"client_id": client_id, "client_secret": client_secret, "code": code},
            headers={"Accept": "application/json"},
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()

        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail=f"GitHub OAuth failed: {token_data.get('error_description', 'unknown error')}")

        # Step 2: fetch user profile
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        user_resp.raise_for_status()
        user_data = user_resp.json()

        # Step 3: fetch primary email if profile email is private
        email = user_data.get("email")
        if not email:
            email_resp = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            if email_resp.status_code == 200:
                emails = email_resp.json()
                primary = next((e["email"] for e in emails if e.get("primary") and e.get("verified")), None)
                email = primary

    return {
        "github_id": user_data["id"],
        "github_login": user_data["login"],
        "email": email,
    }
