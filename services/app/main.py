from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

logger = logging.getLogger("demo.app")

DEFAULT_FLAGS_PATH = Path("/runtime/flags.json")


def load_runtime_flags(path: str | Path | None = None) -> dict[str, Any]:
    flags_path = Path(path or os.environ.get("RUNTIME_FLAGS_PATH", DEFAULT_FLAGS_PATH))
    if not flags_path.exists():
        return {"crash_on_start": False}

    with flags_path.open("r", encoding="utf-8") as handle:
        flags = json.load(handle)

    if not isinstance(flags, dict):
        raise ValueError(f"runtime flags must be a JSON object: {flags_path}")
    flags.setdefault("crash_on_start", False)
    return flags


@asynccontextmanager
async def lifespan(app: FastAPI):
    flags = load_runtime_flags()
    app.state.runtime_flags = flags
    if flags.get("crash_on_start") is True:
        logger.error("crash_on_start flag is enabled; exiting during startup")
        raise RuntimeError("crash_on_start flag is enabled")
    yield


app = FastAPI(title="Incident Demo App", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "service": "app",
        "crash_on_start": app.state.runtime_flags.get("crash_on_start", False),
    }
