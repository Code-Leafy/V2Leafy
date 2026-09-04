"""
V2Leafy Gunicorn multi-worker template (item 34).

Usage (Linux hosts with multiple cores):
    gunicorn -c gunicorn_config.py main:app

WARNING — read before enabling workers > 1:
  * V2Leafy keeps admin sessions, in-memory state and the proxy connection table
    per-process. With more than one worker, an admin request may land on a
    different worker than the one holding the session token (401s), and state
    written by worker A is invisible to worker B.
  * On Railway the state store is memory-only (persistence_mode=memory), so
    multi-worker is NOT supported there: only the worker that handled the last
    write owns the state. Keep `workers = 1` on Railway.
  * On hosts with persistent storage (Codespaces/local), multiple workers share
    unified_state.json with last-write-wins semantics; concurrent writes can be
    lost. Prefer a single worker unless you accept this or add a shared store.

This template deliberately defaults to 1 worker. Raise `workers` only when you
have moved sessions/state to a shared backend (e.g. Redis) or pinned traffic to
a single worker via a load balancer.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = int(os.environ.get("GUNICORN_WORKERS", "1"))
worker_class = "uvicorn.workers.UvicornWorker"
accesslog = None
timeout = 120
graceful_timeout = 15
keepalive = int(os.environ.get("TCP_IDLE_TIMEOUT", "300")) + 30