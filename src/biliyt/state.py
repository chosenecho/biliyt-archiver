"""Persistent workflow state: archive tracking, failed queue, and run reports."""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


class SyncLockError(RuntimeError):
    """Raised when another biliyt sync process already holds the state lock."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class FailedRecord:
    target_id: str
    platform: str
    url: str
    video_id: str | None = None
    title: str | None = None
    error: str = ""
    attempts: int = 1
    failed_at: str = field(default_factory=_utc_now_iso)
    item_type: str = "video"
    extra: dict[str, object] = field(default_factory=dict)


@dataclass
class TargetRunSummary:
    target_id: str
    target_name: str
    platform: str
    url: str
    discovered: int = 0
    already_archived: int = 0
    filtered_out: int = 0
    downloaded: int = 0
    failed: int = 0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)
    module_stats: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass
class RunReport:
    started_at: str
    finished_at: str
    mode: str
    targets: list[TargetRunSummary] = field(default_factory=list)


class StateStore:
    """Manages per-target yt-dlp archives, failed download queue, and JSON reports."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.archives_dir = state_dir / "archives"
        self.reports_dir = state_dir / "reports"
        self.failed_file = state_dir / "failed.jsonl"
        self.last_run_file = state_dir / "last_run.json"
        self.lock_file = state_dir / ".sync.lock"
        self._lock = threading.RLock()

    def ensure_dirs(self) -> None:
        self.archives_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def acquire_sync_lock(self) -> Iterator[Path]:
        """Acquire a non-blocking cross-platform process lock on `state/.sync.lock`."""
        self.ensure_dirs()
        fh = self.lock_file.open("a+b")
        locked = False
        try:
            fh.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                holder = ""
                try:
                    fh.seek(0)
                    holder = fh.read(256).decode("utf-8", errors="ignore").strip()
                except Exception:
                    holder = ""
                detail = f" ({holder})" if holder else ""
                raise SyncLockError(
                    f"检测到另一个 biliyt sync 进程正在运行{detail}，请等待其结束后再试。"
                ) from exc

            fh.seek(0)
            fh.truncate()
            fh.write(f"pid={os.getpid()} started_at={_utc_now_iso()}\n".encode("utf-8"))
            fh.flush()
            yield self.lock_file
        finally:
            if locked:
                try:
                    fh.seek(0)
                    if sys.platform == "win32":
                        import msvcrt

                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            fh.close()

    def archive_path(self, target_id: str) -> Path:
        self.ensure_dirs()
        return self.archives_dir / f"{target_id}.txt"

    def read_archive_entries(self, target_id: str) -> set[str]:
        with self._lock:
            path = self.archives_dir / f"{target_id}.txt"
            if not path.exists():
                return set()
            entries: set[str] = set()
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped:
                    entries.add(stripped)
            return entries

    def append_archive_entries(self, target_id: str, entries: Iterable[str]) -> list[str]:
        """Thread-safely append deduplicated `<extractor> <id>` entries to the target archive."""
        with self._lock:
            existing = self.read_archive_entries(target_id)
            added: list[str] = []
            for raw in entries:
                stripped = raw.strip()
                if stripped and stripped not in existing:
                    existing.add(stripped)
                    added.append(stripped)
            if added:
                path = self.archive_path(target_id)
                with path.open("a", encoding="utf-8") as fh:
                    for line in added:
                        fh.write(f"{line}\n")
            return added

    def read_archive_video_ids(self, target_id: str) -> set[str]:
        """Return bare video IDs extracted from '<extractor> <id>' lines."""
        ids: set[str] = set()
        for entry in self.read_archive_entries(target_id):
            parts = entry.split(maxsplit=1)
            if len(parts) == 2:
                ids.add(parts[1])
            elif parts:
                ids.add(parts[0])
        return ids

    def namespace_archive_path(self, target_id: str, namespace: str) -> Path:
        self.ensure_dirs()
        return self.archives_dir / f"{target_id}.{namespace}.txt"

    def read_namespace_archive_meta(self, target_id: str, namespace: str) -> dict[str, str]:
        """Return `{key: meta}` map from '<target_id>.<namespace>.txt'."""
        with self._lock:
            path = self.archives_dir / f"{target_id}.{namespace}.txt"
            if not path.exists():
                return {}
            result: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split(maxsplit=1)
                key = parts[0]
                meta = parts[1].strip() if len(parts) == 2 else ""
                result[key] = meta
            return result

    def read_namespace_archive(self, target_id: str, namespace: str) -> set[str]:
        """Return archived keys from '<target_id>.<namespace>.txt' (first whitespace token per line)."""
        return set(self.read_namespace_archive_meta(target_id, namespace).keys())

    def append_namespace_archive(
        self,
        target_id: str,
        namespace: str,
        key: str,
        *,
        meta: str = "",
        update_meta: bool = False,
    ) -> None:
        """Append a single archived key if absent, or update its metadata when `update_meta=True`."""
        with self._lock:
            existing = self.read_namespace_archive_meta(target_id, namespace)
            clean_meta = meta.strip()
            if key in existing:
                if not update_meta or existing[key] == clean_meta:
                    return
                existing[key] = clean_meta
                path = self.namespace_archive_path(target_id, namespace)
                lines = [f"{k} {v}".strip() if v else k for k, v in existing.items()]
                path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
                return

            path = self.namespace_archive_path(target_id, namespace)
            line = f"{key} {clean_meta}".strip() if clean_meta else key
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"{line}\n")

    def count_namespace_archive(self, target_id: str, namespace: str) -> int:
        return len(self.read_namespace_archive(target_id, namespace))

    def load_failed(self, target_id: str | None = None) -> list[FailedRecord]:
        with self._lock:
            if not self.failed_file.exists():
                return []
            records: list[FailedRecord] = []
            for line in self.failed_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    rec = FailedRecord(**data)
                except Exception:
                    continue
                if target_id is None or rec.target_id == target_id:
                    records.append(rec)
            return records

    def _write_failed(self, records: list[FailedRecord]) -> None:
        with self._lock:
            self.ensure_dirs()
            content = "\n".join(json.dumps(asdict(r), ensure_ascii=False) for r in records)
            self.failed_file.write_text(f"{content}\n" if content else "", encoding="utf-8")

    def record_failure(
        self,
        *,
        target_id: str,
        platform: str,
        url: str,
        error: str,
        video_id: str | None = None,
        title: str | None = None,
        item_type: str = "video",
        extra: dict[str, object] | None = None,
    ) -> FailedRecord:
        with self._lock:
            all_records = self.load_failed()
            key = (target_id, item_type, url)
            updated: FailedRecord | None = None
            new_records: list[FailedRecord] = []

            for rec in all_records:
                if (rec.target_id, rec.item_type, rec.url) == key:
                    merged_extra = dict(rec.extra)
                    if extra:
                        merged_extra.update(extra)
                    updated = FailedRecord(
                        target_id=target_id,
                        platform=platform,
                        url=url,
                        video_id=video_id or rec.video_id,
                        title=title or rec.title,
                        error=error,
                        attempts=rec.attempts + 1,
                        failed_at=_utc_now_iso(),
                        item_type=item_type,
                        extra=merged_extra,
                    )
                    new_records.append(updated)
                else:
                    new_records.append(rec)

            if updated is None:
                updated = FailedRecord(
                    target_id=target_id,
                    platform=platform,
                    url=url,
                    video_id=video_id,
                    title=title,
                    error=error,
                    attempts=1,
                    failed_at=_utc_now_iso(),
                    item_type=item_type,
                    extra=dict(extra or {}),
                )
                new_records.append(updated)

            self._write_failed(new_records)
            return updated

    def remove_failure(
        self,
        target_id: str,
        url: str,
        *,
        item_type: str | None = None,
    ) -> None:
        with self._lock:
            all_records = self.load_failed()
            remaining = [
                r
                for r in all_records
                if not (
                    r.target_id == target_id
                    and r.url == url
                    and (item_type is None or r.item_type == item_type)
                )
            ]
            if len(remaining) != len(all_records):
                self._write_failed(remaining)

    def save_report(self, report: RunReport) -> Path:
        self.ensure_dirs()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = self.reports_dir / f"run_{ts}.json"
        payload = json.dumps(asdict(report), ensure_ascii=False, indent=2)
        report_path.write_text(payload, encoding="utf-8")
        self.last_run_file.write_text(payload, encoding="utf-8")
        return report_path

    def load_last_report(self) -> dict | None:
        if not self.last_run_file.exists():
            return None
        try:
            return json.loads(self.last_run_file.read_text(encoding="utf-8"))
        except Exception:
            return None
