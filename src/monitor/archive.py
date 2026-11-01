"""Compressed archives of original feed payloads and article pages.

Each kind has one file per hour, compressed as a single stream so that similar
pages compress against each other. A record is one line of JSON giving
`n_bytes`, followed by exactly that many bytes of the original body, so
non-text bytes survive unchanged.

Closed files end in `.rec.zst`; the file being written ends in `.part`. A file
left open by a crash is readable up to its last flush.
"""

from __future__ import annotations

import hashlib
import io
import json
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import zstandard

from monitor.disk import free_gb
from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

QUEUE_SIZE = 2000
STOP = object()


@dataclass
class _Stream:
    hour: str
    part_path: Path
    final_path: Path
    handle: BinaryIO
    writer: Any
    last_flush: float


def final_name(kind: str, hour: str, run: str) -> str:
    return f"{kind}-{hour}-{run}.rec.zst"


def read_archive(path: Path) -> Iterator[tuple[dict[str, Any], bytes]]:
    """Yield (header, body) records, stopping quietly at a truncated end."""
    with path.open("rb") as raw:
        reader = zstandard.ZstdDecompressor().stream_reader(raw)
        buffered = io.BufferedReader(reader)
        try:
            while True:
                line = buffered.readline()
                if not line:
                    return
                header = json.loads(line)
                body = buffered.read(header["n_bytes"])
                if len(body) < header["n_bytes"]:
                    return
                buffered.read(1)
                yield header, body
        except (zstandard.ZstdError, json.JSONDecodeError):
            return


class ArchiveWriter:
    """Accepts records from any thread and writes them from one background one.

    `add` never blocks collection. If the queue is full or the disk is low, a
    record is dropped and counted.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.options = settings.archive
        self.directory = settings.paths.archive
        self.run = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        self.queue: queue.Queue[Any] = queue.Queue(maxsize=QUEUE_SIZE)
        self.streams: dict[str, _Stream] = {}
        self.last_hash: dict[str, str] = {}
        self.thread: threading.Thread | None = None
        self.dropped = 0
        self.unchanged = 0
        self.records = 0
        self.bytes_in = 0

    def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._recover()
        self.thread = threading.Thread(
            target=self._loop, name="archive", daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        if self.thread is None:
            return
        self.queue.put(STOP)
        self.thread.join(timeout=30)
        self.thread = None

    def _recover(self) -> None:
        """Close files a previous run left open."""
        for part in self.directory.glob("*/*.part"):
            part.rename(part.with_suffix(""))
            log.info("archive_recovered", path=str(part))

    def has_room(self) -> bool:
        """With nowhere to offload to, stop before the disk gets tight."""
        if self.options.s3_bucket:
            return free_gb(self.directory) >= self.settings.disk.degrade_gb
        return free_gb(self.directory) >= self.options.min_free_gb

    def add(
        self,
        kind: str,
        key: str,
        source_id: str,
        body: bytes,
        meta: dict[str, Any] | None = None,
        skip_unchanged: bool = False,
    ) -> dict[str, Any] | None:
        """Queue one body. Returns its index row, or None if not stored."""
        if not self.options.enabled:
            return None
        digest = hashlib.sha256(body).hexdigest()
        if skip_unchanged and self.last_hash.get(key) == digest:
            self.unchanged += 1
            return None
        if not self.has_room():
            self.dropped += 1
            return None
        now = datetime.now(UTC)
        hour = now.strftime("%Y%m%dT%H")
        header = {
            "kind": kind,
            "key": key,
            "source_id": source_id,
            "fetched_at": now.isoformat(),
            "sha256": digest,
            "n_bytes": len(body),
            **(meta or {}),
        }
        try:
            self.queue.put_nowait((kind, hour, header, body))
        except queue.Full:
            self.dropped += 1
            return None
        if skip_unchanged:
            self.last_hash[key] = digest
        return {
            "kind": kind,
            "key": key,
            "source_id": source_id,
            "fetched_at": now,
            "sha256": digest,
            "n_bytes": len(body),
            "archive_file": final_name(kind, hour, self.run),
        }

    def _open(self, kind: str, hour: str) -> _Stream:
        folder = self.directory / kind
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / final_name(kind, hour, self.run)
        part = final.with_name(final.name + ".part")
        handle = part.open("ab")
        parameters = zstandard.ZstdCompressionParameters.from_level(
            self.options.compression_level,
            window_log=self.options.window_log,
            enable_ldm=True,
        )
        compressor = zstandard.ZstdCompressor(compression_params=parameters)
        return _Stream(
            hour,
            part,
            final,
            handle,
            compressor.stream_writer(handle),
            time.monotonic(),
        )

    @staticmethod
    def _close(stream: _Stream) -> None:
        stream.writer.close()
        stream.handle.close()
        stream.part_path.replace(stream.final_path)

    def _flush_due(self) -> None:
        now = time.monotonic()
        interval = self.options.flush_seconds
        for stream in self.streams.values():
            if now - stream.last_flush >= interval:
                stream.writer.flush(zstandard.FLUSH_BLOCK)
                stream.handle.flush()
                stream.last_flush = now

    def _write(
        self, kind: str, hour: str, header: dict[str, Any], body: bytes
    ) -> None:
        stream = self.streams.get(kind)
        if stream is not None and stream.hour != hour:
            self._close(stream)
            stream = None
        if stream is None:
            stream = self._open(kind, hour)
            self.streams[kind] = stream
        stream.writer.write(json.dumps(header).encode() + b"\n")
        stream.writer.write(body)
        stream.writer.write(b"\n")
        self.records += 1
        self.bytes_in += len(body)

    def _loop(self) -> None:
        while True:
            try:
                entry = self.queue.get(timeout=1.0)
            except queue.Empty:
                self._flush_due()
                continue
            if entry is STOP:
                break
            try:
                self._write(*entry)
                self._flush_due()
            except OSError as error:
                self.dropped += 1
                log.error("archive_write_failed", error=str(error)[:200])
        for stream in self.streams.values():
            self._close(stream)
        self.streams.clear()

    def status(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "bytes_in": self.bytes_in,
            "unchanged": self.unchanged,
            "dropped": self.dropped,
        }
