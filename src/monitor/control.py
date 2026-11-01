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
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from monitor.lake import WriterBusy, writer_lock
from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

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
    if command == "relabel":
        raise ValueError("relabel needs a running pipeline")
    raise ValueError(f"unknown command: {command}")


def run_or_request(settings: Settings, command: str, **options: Any) -> str:
    """Run a write command in the pipeline if it is up, otherwise directly.

    A direct run holds the writer lock, so a pipeline cannot start under it.
    """
    try:
        with writer_lock(settings):
            return _direct(settings, command, **options)
    except WriterBusy:
        pass
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


class SpoolWriter:
    """Holds lake rows from a CLI process until the pipeline can write them.

    It has the same `add` as `BatchWriter`, so a command that calls Claude can
    record its ledger rows without writing to the lake itself. Each call writes
    one file, which appears under its final name only when complete.
    """

    def __init__(self, settings: Settings) -> None:
        settings.paths.control.mkdir(parents=True, exist_ok=True)
        self.control = settings.paths.control

    def add(self, table: str, rows: Iterable[dict[str, Any]]) -> None:
        name = uuid.uuid4().hex
        temporary = self.control / f"{name}.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            for row in rows:
                record = {"table": table, "row": row}
                handle.write(json.dumps(record, default=str) + "\n")
        os.replace(temporary, self.control / f"{name}.spool.jsonl")


def _read_spool(path: Path) -> dict[str, list[Any]]:
    by_table: dict[str, list[Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        row = record["row"]
        for key, value in row.items():
            if key.endswith("_at") and isinstance(value, str):
                row[key] = datetime.fromisoformat(value)
        by_table.setdefault(record["table"], []).append(row)
    return by_table


def drain_spool(control: Path, add: Callable[[str, list[Any]], None]) -> int:
    """Move spooled rows into the writer. Called from inside the pipeline.

    A file that cannot be read is set aside, so it cannot hold back the rest.
    """
    if not control.exists():
        return 0
    moved = 0
    for path in sorted(control.glob("*.spool.jsonl")):
        try:
            by_table = _read_spool(path)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            path.rename(path.with_name(path.name.split(".")[0] + ".spool.bad"))
            log.error(
                "spool_set_aside", path=str(path), error=str(error)[:200]
            )
            continue
        for table, rows in by_table.items():
            add(table, rows)
            moved += len(rows)
        path.unlink()
    return moved


def _reply(control: Path, request_id: str, reply: dict[str, Any]) -> None:
    temporary = control / f"{request_id}.tmp"
    temporary.write_text(json.dumps(reply))
    os.replace(temporary, control / f"{request_id}.result.json")


async def process_async_requests(
    control: Path, handlers: dict[str, Callable[..., Awaitable[str]]]
) -> int:
    """Run pending requests whose handlers call the API."""
    if not control.exists():
        return 0
    handled = 0
    for request in sorted(control.glob("*.request.json")):
        payload = json.loads(request.read_text())
        handler = handlers.get(payload["command"])
        if handler is None:
            continue
        request_id = request.name.removesuffix(".request.json")
        request.unlink()
        try:
            reply = {"result": await handler(**payload.get("options", {}))}
        except Exception as error:
            reply = {"error": f"{type(error).__name__}: {error}"}
        _reply(control, request_id, reply)
        handled += 1
    return handled


def process_requests(control: Path, handlers: dict[str, Handler]) -> int:
    """Run pending requests. Called from inside the pipeline."""
    if not control.exists():
        return 0
    handled = 0
    for request in sorted(control.glob("*.request.json")):
        request_id = request.name.removesuffix(".request.json")
        payload = json.loads(request.read_text())
        if payload["command"] not in handlers:
            continue
        request.unlink()
        reply: dict[str, Any]
        try:
            handler = handlers[payload["command"]]
            reply = {"result": handler(**payload.get("options", {}))}
        except Exception as error:
            reply = {"error": f"{type(error).__name__}: {error}"}
        _reply(control, request_id, reply)
        handled += 1
    return handled
