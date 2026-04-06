"""
FastAPI application factory for the LittleProteinTiger web platform.

Start the development server:
    uvicorn web.backend.app:app --reload --port 8000

Start the Celery worker (separate terminal):
    celery -A web.backend.celery_app worker --loglevel=info --concurrency=2

Required environment variables (copy .env.example to .env):
    JWT_SECRET            — random string, e.g. `openssl rand -hex 32`
    FERNET_KEY            — Fernet key for API key encryption
    GITHUB_CLIENT_ID      — GitHub OAuth app client ID
    GITHUB_CLIENT_SECRET  — GitHub OAuth app client secret
    REDIS_URL             — Redis broker URL (default: redis://localhost:6379/0)
    FRONTEND_URL          — React dev server or production URL (default: http://localhost:5173)
    ANTHROPIC_API_KEY     — Optional fallback key; per-user BYOK takes precedence
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from web.backend.db import init_db
from web.backend.routers import auth, projects, runs, structures


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="LittleProteinTiger API",
        version="0.1.0",
        lifespan=_lifespan,
        # Hide docs in production
        docs_url="/docs" if os.environ.get("ENV") != "production" else None,
        redoc_url=None,
    )

    frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:5173")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[frontend_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(runs.router)
    app.include_router(structures.router)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
