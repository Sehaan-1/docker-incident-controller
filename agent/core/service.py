from __future__ import annotations

import os
import threading

import uvicorn

from agent.core.worker import run_polling_loop
from agent.core.logs import setup_logging
from agent.storage.sqlite_store import SQLiteStore


def main() -> int:
    setup_logging()

    stop_event = threading.Event()
    worker = threading.Thread(
        target=run_polling_loop,
        kwargs={"store": SQLiteStore.from_env(), "stop_event": stop_event},
        name="incident-worker",
        daemon=True,
    )
    worker.start()

    try:
        uvicorn.run(
            "agent.api.main:app",
            host=os.environ.get("API_HOST", "0.0.0.0"),
            port=int(os.environ.get("API_PORT", "8000")),
            log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"),
        )
    finally:
        stop_event.set()
        worker.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
