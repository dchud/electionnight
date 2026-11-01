"""Copy archives and catalog backups to a private S3 bucket.

Nothing happens until a bucket and keys are configured. A local archive file is
deleted only after its upload has been checked and it has passed the keep age.
The configured credentials cannot delete anything from the bucket.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)


def is_configured(settings: Settings) -> bool:
    options = settings.archive
    return bool(
        options.s3_bucket
        and options.s3_access_key_id
        and options.s3_secret_access_key
    )


def build_client(settings: Settings) -> Any:
    import boto3

    options = settings.archive
    key_id = options.s3_access_key_id
    secret = options.s3_secret_access_key
    if key_id is None or secret is None:
        raise ValueError("S3 keys are not configured")
    return boto3.client(
        "s3",
        region_name=options.s3_region,
        aws_access_key_id=key_id.get_secret_value(),
        aws_secret_access_key=secret.get_secret_value(),
    )


@dataclass
class OffloadResult:
    uploaded: int = 0
    bytes_uploaded: int = 0
    deleted_local: int = 0
    failed: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"uploaded {self.uploaded} files, "
            f"{self.bytes_uploaded / 1024**2:.1f} MB; "
            f"deleted {self.deleted_local} local; "
            f"{len(self.failed)} failed"
        )


class Offloader:
    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        self.options = settings.archive
        self.client = client
        self.manifest_path = settings.paths.archive / "uploaded.jsonl"

    def _client(self) -> Any:
        if self.client is None:
            self.client = build_client(self.settings)
        return self.client

    def _uploaded(self) -> dict[str, int]:
        if not self.manifest_path.exists():
            return {}
        entries = {}
        for line in self.manifest_path.read_text().splitlines():
            record = json.loads(line)
            entries[record["key"]] = record["size"]
        return entries

    def _record(self, key: str, size: int) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("a") as handle:
            handle.write(json.dumps({"key": key, "size": size}) + "\n")

    def _object_key(self, path: Path, folder: str) -> str:
        return f"{self.options.s3_prefix}/{folder}/{path.name}"

    def _upload(self, path: Path, key: str, result: OffloadResult) -> bool:
        """Upload one file and confirm the stored size matches."""
        size = path.stat().st_size
        client = self._client()
        try:
            client.upload_file(str(path), self.options.s3_bucket, key)
            stored = client.head_object(Bucket=self.options.s3_bucket, Key=key)
        except Exception as error:
            result.failed.append(path.name)
            log.warning(
                "offload_failed",
                file=path.name,
                error=type(error).__name__,
            )
            return False
        if stored["ContentLength"] != size:
            result.failed.append(path.name)
            log.warning("offload_size_mismatch", file=path.name)
            return False
        self._record(key, size)
        result.uploaded += 1
        result.bytes_uploaded += size
        return True

    def closed_archives(self) -> list[tuple[Path, str]]:
        directory = self.settings.paths.archive
        return [
            (path, f"archive/{path.parent.name}")
            for path in sorted(directory.glob("*/*.rec.zst"))
        ]

    def run(self, now: float | None = None) -> OffloadResult:
        """Upload what is new, then delete old local archives already safe."""
        result = OffloadResult()
        if not is_configured(self.settings):
            return result
        done = self._uploaded()
        pending = list(self.closed_archives())
        backups = sorted(self.settings.paths.backups.glob("catalog-*.zst"))
        if backups:
            pending.append((backups[-1], "backups"))
        for path, folder in pending:
            key = self._object_key(path, folder)
            if done.get(key) == path.stat().st_size:
                continue
            if self._upload(path, key, result):
                done[key] = path.stat().st_size
        keep = self.options.local_keep_hours * 3600
        moment = now if now is not None else time.time()
        for path, folder in self.closed_archives():
            key = self._object_key(path, folder)
            age = moment - path.stat().st_mtime
            if done.get(key) == path.stat().st_size and age >= keep:
                path.unlink()
                result.deleted_local += 1
        return result

    def upload_lake(self) -> OffloadResult:
        """Upload current Parquet files once, on request.

        Merges rewrite files, so doing this continuously would upload many
        copies of the same rows.
        """
        result = OffloadResult()
        if not is_configured(self.settings):
            result.failed.append("no bucket configured")
            return result
        done = self._uploaded()
        root = self.settings.paths.data
        for path in sorted(root.rglob("*.parquet")):
            relative = path.relative_to(root).as_posix()
            key = f"{self.options.s3_prefix}/lake/{relative}"
            if done.get(key) != path.stat().st_size:
                self._upload(path, key, result)
        return result
