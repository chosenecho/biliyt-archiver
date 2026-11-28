"""Portable, per-item artifact manifests and reconciliation of incomplete archives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .atomic_io import atomic_write_json

ArtifactStatus = Literal["complete", "failed", "pending", "disabled", "unavailable", "unknown"]
_STATUSES = {"complete", "failed", "pending", "disabled", "unavailable", "unknown"}
_KINDS = {"video", "dynamic", "article", "profile", "favorite", "comment"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(path: Path) -> Path:
    resolved = path.resolve()
    # Windows realpath can retain the extended-length prefix for a missing
    # suffix during concurrent directory creation. Normalize both equivalent
    # spellings after resolving symlinks, including extended UNC names.
    if os.name == "nt":
        text = str(resolved)
        if text.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + text[8:])
        if text.startswith("\\\\?\\"):
            return Path(text[4:])
    return resolved


def public_url(value: str) -> str:
    """Keep only public source identifiers, never credentials or signed query parameters."""
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return ""
        host = parts.hostname
        port = parts.port  # Access validates malformed and out-of-range ports.
        if ":" in host:
            host = f"[{host}]"
        if port is not None:
            host = f"{host}:{port}"
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k in {"v", "p"}])
        return urlunsplit((parts.scheme, host, parts.path, query, ""))
    except ValueError:
        return ""


def clean_error(error: object) -> str:
    text = str(error)
    text = re.sub(r"https?://[^\s<>\"']+", "[URL]", text)
    # Header values may contain several cookies or a spaced Bearer credential.
    text = re.sub(
        r"(?im)\b(cookie|set-cookie|authorization|proxy-authorization)\b[\"']?\s*[:=]\s*[^\r\n]+",
        r"\1: [REDACTED]", text,
    )
    text = re.sub(
        r"(?i)\b(SESSDATA|bili_jct|DedeUserID|buvid\w*|token|access_token|refresh_token|"
        r"password|passwd|secret|api_key|signature|sign)\b[\"']?(\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}&]+)",
        r"\1\2[REDACTED]", text,
    )
    return text[:1500]


@dataclass
class Artifact:
    kind: str
    status: ArtifactStatus = "pending"
    path: str | None = None
    source_url: str = ""
    language: str | None = None
    error: str = ""


@dataclass
class ArtifactManifest:
    target_id: str
    kind: str
    item_id: str
    source_url: str = ""
    artifacts: list[Artifact] = field(default_factory=list)
    index_status: ArtifactStatus = "pending"
    index_error: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=utc_now)
    schema_version: int = 1

    @property
    def partial(self) -> bool:
        return any(a.status in {"failed", "pending"} for a in self.artifacts)

    @property
    def needs_repair(self) -> bool:
        return self.partial or self.index_status in {"failed", "pending"}


@dataclass
class DownloadResult:
    """Final paths collected after yt-dlp's move/merge postprocessing."""
    item_id: str
    media_path: Path | None = None
    info_path: Path | None = None
    subtitle_paths: dict[str, Path] = field(default_factory=dict)
    thumbnail_path: Path | None = None
    requested_languages: list[str] = field(default_factory=list)


class ArtifactStore:
    """Keep item manifests below one target root; all persisted paths are relative."""

    _write_lock = threading.RLock()

    def __init__(self, root: Path, target_id: str) -> None:
        self.root = _canonical(root)
        self.target_id = target_id
        self.directory = self.root / ".biliyt" / "manifests"
        self._lock = self._write_lock

    def path_for(self, kind: str, item_id: str) -> Path:
        if not isinstance(kind, str) or kind not in _KINDS or not isinstance(item_id, str) or not item_id:
            raise ValueError("Invalid manifest kind or empty item ID")
        digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
        path = self.directory / kind / f"{digest}.json"
        self.relative(path)  # Also reject symlinked manifest directories escaping the root.
        return path

    def resolve(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative or "\x00" in relative:
            raise ValueError("Manifest path must be a nonempty relative POSIX string")
        p = Path(relative)
        if (p.is_absolute() or "\\" in relative or ":" in relative
                or any(part in {"", ".", ".."} for part in relative.split("/"))):
            raise ValueError("Manifest path must be relative POSIX without traversal")
        resolved = _canonical(self.root / p)
        if not resolved.is_relative_to(_canonical(self.root)):
            raise ValueError("Manifest path escapes archive root")
        return resolved

    def relative(self, path: Path) -> str:
        resolved = _canonical(path)
        # Re-resolve the root as well: 8.3 expansion depends on directory existence.
        root = _canonical(self.root)
        if not resolved.is_relative_to(root):
            raise ValueError("Artifact is outside target download directory")
        return resolved.relative_to(root).as_posix()

    def _validate(self, manifest: ArtifactManifest) -> None:
        if (manifest.target_id != self.target_id or type(manifest.schema_version) is not int
                or manifest.schema_version != 1):
            raise ValueError("Manifest target or schema version mismatch")
        self.path_for(manifest.kind, manifest.item_id)
        if not isinstance(manifest.index_status, str) or manifest.index_status not in _STATUSES:
            raise ValueError("Invalid index status")
        if not isinstance(manifest.artifacts, list):
            raise ValueError("Manifest artifacts must be a list")
        for artifact in manifest.artifacts:
            if not isinstance(artifact, Artifact) or not isinstance(artifact.kind, str) or not artifact.kind:
                raise ValueError("Invalid artifact")
            if not isinstance(artifact.status, str) or artifact.status not in _STATUSES:
                raise ValueError("Invalid artifact status")
            if artifact.path is not None:
                self.resolve(artifact.path)
            artifact.source_url = public_url(artifact.source_url)
            artifact.error = clean_error(artifact.error)
        # Context holds render/index coordinates only, never arbitrary metadata.
        allowed = {"metadata_path", "markdown_path", "image_dir", "carrier_kind", "oid", "rtype", "title"}
        if not isinstance(manifest.context, dict) or set(manifest.context) - allowed:
            raise ValueError("Unexpected manifest context fields")
        for key, value in manifest.context.items():
            if key in {"metadata_path", "markdown_path", "image_dir"}:
                self.resolve(value)
            elif key == "rtype":
                if type(value) is not int or value < 0:
                    raise ValueError("Manifest rtype must be a nonnegative integer")
            elif not isinstance(value, str):
                raise ValueError(f"Manifest context {key} must be a string")
        manifest.source_url = public_url(manifest.source_url)
        manifest.index_error = clean_error(manifest.index_error)

    def save(self, manifest: ArtifactManifest) -> None:
        with self._lock:
            self._validate(manifest)
            manifest.updated_at = utc_now()
            atomic_write_json(self.path_for(manifest.kind, manifest.item_id), asdict(manifest))

    def _read(self, path: Path) -> ArtifactManifest:
        self.relative(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Manifest must be a JSON object")
        if "schema_version" not in data or "target_id" not in data:
            raise ValueError("Missing manifest schema/target")
        try:
            data["artifacts"] = [Artifact(**a) for a in data.get("artifacts", [])]
            manifest = ArtifactManifest(**data)
        except TypeError as exc:
            raise ValueError("Invalid manifest structure") from exc
        self._validate(manifest)
        if self.path_for(manifest.kind, manifest.item_id).absolute() != path.absolute():
            raise ValueError("Manifest identity mismatch")
        for artifact in manifest.artifacts:
            if artifact.status == "complete" and (artifact.path is None or not self.resolve(artifact.path).is_file()):
                artifact.status = "pending"
                artifact.error = "Local artifact is missing"
                manifest.index_status = "pending"
        return manifest

    def load(self, kind: str, item_id: str) -> ArtifactManifest | None:
        path = self.path_for(kind, item_id)
        return self._read(path) if path.exists() else None

    def manifests(self, *, errors: list[str] | None = None) -> list[ArtifactManifest]:
        """Read all valid manifests, optionally collecting sanitized per-file failures."""
        found = []
        if self.directory.exists():
            for path in sorted(self.directory.glob("*/*.json")):
                try:
                    found.append(self._read(path))
                except (OSError, ValueError, TypeError) as exc:
                    if errors is not None:
                        errors.append(clean_error(f"Manifest {path.name}: {exc}"))
        return found

    def stats(self, *, errors: list[str] | None = None) -> dict[str, int]:
        manifests = self.manifests(errors=errors)
        return {
            "partial": sum(m.partial for m in manifests),
            "index_failed": sum(m.index_status in {"pending", "failed"} for m in manifests),
            "pending_repair": sum(m.needs_repair for m in manifests),
        }

    def import_legacy_videos(
        self, *, persist: bool = False, errors: list[str] | None = None,
    ) -> list[ArtifactManifest]:
        """Discover old metadata offline. Unknown historical attachment intent stays unknown.

        With ``persist=False`` this is a pure read, including when files are malformed.
        Duplicate item IDs are discovered once per call; saved manifests suppress later imports.
        """
        found = []
        seen: set[str] = set()
        for path in sorted(self.root.rglob("*.info.json")) if self.root.exists() else []:
            if ".biliyt" in path.relative_to(self.root).parts:
                continue
            try:
                metadata_path = self.relative(path)
                info = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(info, dict):
                    raise ValueError("Legacy metadata must be a JSON object")
                item_id = str(info.get("id") or "")
                if not item_id or item_id in seen:
                    continue
                if self.load("video", item_id):
                    seen.add(item_id)
                    continue
                stem = path.name[:-len(".info.json")]
                files = [Artifact("metadata", "complete", metadata_path), Artifact("subtitle", "unknown")]
                for ext in ("mp4", "mkv", "webm"):
                    media = path.with_name(f"{stem}.{ext}")
                    if media.is_file():
                        files.append(Artifact("media", "complete", self.relative(media)))
                        break
                manifest = ArtifactManifest(
                    self.target_id, "video", item_id, public_url(info.get("webpage_url", "")),
                    files, context={"metadata_path": metadata_path},
                )
                if persist:
                    self.save(manifest)
                found.append(manifest)
                seen.add(item_id)
            except (OSError, ValueError, TypeError) as exc:
                if errors is not None:
                    errors.append(clean_error(f"Legacy metadata {path.name}: {exc}"))
        return found
