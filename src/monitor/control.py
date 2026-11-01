"""Route CLI write requests through the pipeline when it is running.

Only one process may write to the lake. A command run while the pipeline holds
the writer lock drops a request file in the control directory and waits for
the result file. With no pipeline running, the command runs directly.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from monitor.lake import writer_is_running
from monitor.settings import Settings

REQUEST_TIMEOUT_SECONDS = 600
POLL_SECONDS = 0.5

Handler = Callable[..., str]


def _direct(settings: Settings, command: str, **options: Any) -> str:
    if command == "prune":
        from monitor.maintenance import prune_standalone

        return prune_standalone(settings, **options)
    if command == "rollups":
        from monitor.rollups import rollups_standalone

        return rollups_standalone(settings, **options)
    if command == "brief":
        from monitor.parsers.briefs import brief_standalone

        return brief_standalone(settings, **options)
    raise ValueError(f"unknown command: {command}")


def run_or_request(settings: Settings, command: str, **options: Any) -> str:
    """Run a write command in the pipeline if it is up, otherwise directly."""
    if not writer_is_running(settings):
        return _direct(settings, command, **options)
    control = settings.paths.control
    control.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    temporary = control / f"{request_id}.tmp"
    temporary.write_text(json.dumps({"command": command, "options": options}))
    os.replace(temporary, control / f"{request_id}.request.json")
    result = control / f"{request_id}.result.json"
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if result.exists():
            payload = json.loads(result.read_text())
            result.unlink()
            if payload.get("error"):
                raise RuntimeError(payload["error"])
            return payload["result"]
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"no reply from the pipeline for {command}")


def process_requests(control: Path, handlers: dict[str, Handler]) -> int:
    """Run pending requests. Called from inside the pipeline."""
    if not control.exists():
        return 0
    handled = 0
    for request in sorted(control.glob("*.request.json")):
        request_id = request.name.removesuffix(".request.json")
        payload = json.loads(request.read_text())
        request.unlink()
        reply: dict[str, Any]
        try:
            handler = handlers[payload["command"]]
            reply = {"result": handler(**payload.get("options", {}))}
        except Exception as error:
            reply = {"error": f"{type(error).__name__}: {error}"}
        temporary = control / f"{request_id}.tmp"
        temporary.write_text(json.dumps(reply))
        os.replace(temporary, control / f"{request_id}.result.json")
        handled += 1
    return handled
