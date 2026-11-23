"""Persistent workflow state: archive tracking, failed queue, and run reports."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


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

    def ensure_dirs(self) -> None:
        self.archives_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def archive_path(self, target_id: str) -> Path:
        self.ensure_dirs()
        return self.archives_dir / f"{target_id}.txt"

    def read_archive_entries(self, target_id: str) -> set[str]:
        path = self.archives_dir / f"{target_id}.txt"
        if not path.exists():
            return set()
        entries: set[str] = set()
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped:
                entries.add(stripped)
        return entries

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

    def read_namespace_archive(self, target_id: str, namespace: str) -> set[str]:
        """Return archived keys from '<target_id>.<namespace>.txt' (first whitespace token per line)."""
        path = self.archives_dir / f"{target_id}.{namespace}.txt"
        if not path.exists():
            return set()
        keys: set[str] = set()
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            keys.add(stripped.split(maxsplit=1)[0])
        return keys

    def append_namespace_archive(
        self,
        target_id: str,
        namespace: str,
        key: str,
        *,
        meta: str = "",
    ) -> None:
        """Append a single archived key if not already present."""
        existing = self.read_namespace_archive(target_id, namespace)
        if key in existing:
            return
        path = self.namespace_archive_path(target_id, namespace)
        line = f"{key} {meta}".strip() if meta else key
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{line}\n")

    def count_namespace_archive(self, target_id: str, namespace: str) -> int:
        return len(self.read_namespace_archive(target_id, namespace))

    def load_failed(self, target_id: str | None = None) -> list[FailedRecord]:
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
