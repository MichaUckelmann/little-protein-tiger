"""
Celery application instance for LittleProteinTiger.

Start a worker with:
    celery -A web.backend.celery_app worker --loglevel=info --concurrency=2

Each pipeline run takes 5-20 min and is I/O-bound on LLM calls.
concurrency=2 means 2 runs can execute in parallel per worker process.
"""
import os
from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "lpt",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["web.backend.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Fetch one task at a time so a slow pipeline doesn't starve other workers
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)
