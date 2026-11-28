"""Offline behavior checks for portable manifests and legacy archive discovery."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest

from biliyt.artifacts import Artifact, ArtifactManifest, ArtifactStore, clean_error, public_url


def manifest(store: ArtifactStore, item_id: str = "video-1") -> ArtifactManifest:
    return ArtifactManifest(store.target_id, "video", item_id)


def put_raw(store: ArtifactStore, value: dict, *, item_id: str = "video-1") -> Path:
    path = store.path_for("video", item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def legacy(root: Path, name: str, item_id: str = "video-1") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.info.json"
    path.write_text(json.dumps({
        "id": item_id,
        "webpage_url": "https://user:password@example.test/watch?v=video-1&signature=private",
    }), encoding="utf-8")
    return path


def test_legacy_discovery_continues_after_bad_json_and_never_writes_by_default(tmp_path):
    store = ArtifactStore(tmp_path, "target")
    (tmp_path / "a.info.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "b.info.json").write_text("[]", encoding="utf-8")
    info = legacy(tmp_path, "valid")
    media = tmp_path / "valid.mkv"
    media.write_bytes(b"media")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    errors = ["existing"]

    found = store.import_legacy_videos(errors=errors)

    assert [m.item_id for m in found] == ["video-1"]
    assert len(errors) == 3
    assert errors[0] == "existing"
    assert found[0].source_url == "https://example.test/watch?v=video-1"
    assert {(a.kind, a.status, a.path) for a in found[0].artifacts} == {
        ("metadata", "complete", info.name), ("media", "complete", media.name),
        ("subtitle", "unknown", None),
    }
    assert not store.directory.exists()
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert len(store.import_legacy_videos()) == 1


@pytest.mark.parametrize("persist", [False, True])
def test_duplicate_legacy_files_discover_one_item_and_saved_items_stay_skipped(tmp_path, persist):
    store = ArtifactStore(tmp_path, "target")
    legacy(tmp_path, "first")
    legacy(tmp_path, "second")
    legacy(tmp_path, "third", "video-2")
    assert [m.item_id for m in store.import_legacy_videos(persist=persist)] == ["video-1", "video-2"]
    assert len(store.manifests()) == (2 if persist else 0)
    assert len(store.import_legacy_videos(persist=persist)) == (0 if persist else 2)


@pytest.mark.parametrize("context", [
    {"cookies": "private"}, {"metadata_path": {"value": "a.json"}},
    {"markdown_path": ""}, {"title": {"token": "private"}}, {"rtype": True},
    {"rtype": "1"}, {"oid": ["1"]}, [],
])
@pytest.mark.parametrize("operation", ["save", "load"])
def test_context_whitelist_and_types_apply_on_read_and_write(tmp_path, context, operation):
    store = ArtifactStore(tmp_path, "target")
    value = manifest(store)
    value.context = context
    if operation == "save":
        with pytest.raises(ValueError):
            store.save(value)
        assert not store.directory.exists()
    else:
        put_raw(store, asdict(value))
        with pytest.raises(ValueError):
            store.load("video", "video-1")


@pytest.mark.parametrize("path", ["../escape", "inside/../file", "/absolute", "C:/file", "a\\file", "./file", "a//b"])
@pytest.mark.parametrize("location", ["artifact", "context"])
@pytest.mark.parametrize("operation", ["save", "load"])
def test_manifest_paths_reject_traversal_and_absolute_names(tmp_path, path, location, operation):
    store = ArtifactStore(tmp_path, "target")
    value = manifest(store)
    if location == "artifact":
        value.artifacts = [Artifact("metadata", path=path)]
    else:
        value.context = {"markdown_path": path}
    if operation == "load":
        put_raw(store, asdict(value))
    with pytest.raises(ValueError):
        store.save(value) if operation == "save" else store.load("video", "video-1")


def test_local_markdown_context_and_portable_files_survive_root_migration(tmp_path):
    root = tmp_path / "original"
    root.mkdir()
    (root / "article.md").write_text("# Offline article", encoding="utf-8")
    (root / "article.json").write_text("{}", encoding="utf-8")
    store = ArtifactStore(root, "target")
    value = ArtifactManifest("target", "article", "cv1", artifacts=[
        Artifact("markdown", "complete", "article.md"), Artifact("metadata", "complete", "article.json"),
    ], index_status="complete", context={
        "markdown_path": "article.md", "metadata_path": "article.json", "image_dir": "images",
        "carrier_kind": "article", "oid": "1", "rtype": 12, "title": "Local article",
    })
    store.save(value)
    moved = tmp_path / "moved"
    shutil.copytree(root, moved)
    moved_store = ArtifactStore(moved, "target")
    loaded = moved_store.load("article", "cv1")
    assert loaded.context == value.context
    assert moved_store.resolve(loaded.context["markdown_path"]).read_text(encoding="utf-8") == "# Offline article"
    assert not loaded.needs_repair
    (moved / "article.md").unlink()
    persisted_before = moved_store.path_for("article", "cv1").read_bytes()
    missing = moved_store.load("article", "cv1")
    assert missing.artifacts[0].status == "pending"
    assert missing.artifacts[1].status == "complete"
    assert missing.index_status == "pending"
    assert missing.needs_repair
    assert moved_store.path_for("article", "cv1").read_bytes() == persisted_before


def test_store_created_before_root_handles_concurrent_directory_creation(tmp_path):
    root = tmp_path / "new archive with long directory name" / "target"
    store = ArtifactStore(root, "target")
    assert not root.exists()

    def archive(index):
        path = root / f"worker-{index}" / "video.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")
        relative = store.relative(path)
        value = manifest(store, str(index))
        value.artifacts = [Artifact("media", "complete", relative)]
        store.save(value)
        assert store.resolve(relative) == path.resolve()
        return value.item_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        completed = set(pool.map(archive, range(24)))
    assert {m.item_id for m in store.manifests()} == completed
    assert all(not m.partial for m in store.manifests())


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length paths")
def test_extended_length_windows_paths_share_the_same_archive_coordinates(tmp_path):
    store = ArtifactStore(tmp_path / "not-created-yet", "target")
    path = store.root / "nested" / "video.mp4"
    extended = Path("\\\\?\\" + str(path))
    assert store.relative(extended) == "nested/video.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"media")
    assert store.relative(extended) == "nested/video.mp4"
    assert store.resolve(store.relative(extended)) == path.resolve()


def test_symlinks_cannot_escape_archive_for_artifacts_or_manifest_storage(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("private", encoding="utf-8")
    def link_directory(path):
        try:
            path.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt":
                pytest.skip(f"Directory symlinks unavailable: {exc}")
            # Junctions exercise the same resolved-path escape without requiring
            # Windows developer mode or the symbolic-link creation privilege.
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(path), str(outside)],
                capture_output=True, check=False,
            )
            if result.returncode:
                pytest.skip("Directory symlinks and junctions unavailable")

    link_directory(root / "link")
    store = ArtifactStore(root, "target")
    for value in [
        ArtifactManifest("target", "video", "video-1", artifacts=[Artifact("markdown", path="link/secret.md")]),
        ArtifactManifest("target", "video", "video-1", context={"markdown_path": "link/secret.md"}),
    ]:
        with pytest.raises(ValueError):
            store.save(value)
        put_raw(store, asdict(value))
        with pytest.raises(ValueError):
            store.load("video", "video-1")
    second_root = tmp_path / "second-archive"
    second_root.mkdir()
    link_directory(second_root / ".biliyt")
    second_store = ArtifactStore(second_root, "target")
    with pytest.raises(ValueError):
        second_store.save(manifest(second_store))
    assert list(outside.iterdir()) == [outside / "secret.md"]


@pytest.mark.parametrize("changes", [
    {"schema_version": 2}, {"schema_version": True}, {"target_id": "other"},
    {"item_id": "other"}, {"kind": "profile"}, {"kind": "bogus"},
    {"index_status": "bogus"}, {"artifacts": [{"kind": "media", "status": "bogus"}]},
])
def test_load_rejects_version_target_identity_and_status_mismatch(tmp_path, changes):
    store = ArtifactStore(tmp_path, "target")
    data = asdict(manifest(store))
    data.update(changes)
    put_raw(store, data)
    with pytest.raises(ValueError):
        store.load("video", "video-1")


def test_batch_manifest_reads_isolate_failures_and_collect_errors(tmp_path):
    store = ArtifactStore(tmp_path, "target")
    store.save(manifest(store, "good"))
    bad_path = store.path_for("video", "bad")
    bad_path.write_text("{broken", encoding="utf-8")
    data = asdict(manifest(store, "sensitive"))
    data["context"] = {"authorization": "Bearer private"}
    put_raw(store, data, item_id="sensitive")
    errors = []
    assert [m.item_id for m in store.manifests(errors=errors)] == ["good"]
    assert len(errors) == 2
    assert "private" not in " ".join(errors)
    assert [m.item_id for m in store.manifests()] == ["good"]
    assert store.stats()["pending_repair"] == 1


@pytest.mark.parametrize("url, expected", [
    ("https://user:secret@example.test:443/watch?v=abc&p=2&token=private#secret", "https://example.test:443/watch?v=abc&p=2"),
    ("https://[::1]:8080/watch?signature=private", "https://[::1]:8080/watch"),
    ("https://example.test:bad/watch", ""), ("https://example.test:99999/watch", ""),
    ("https://[broken/watch", ""), ("file:///private", ""), (None, ""),
])
def test_public_url_handles_invalid_authorities_and_discards_credentials(url, expected):
    assert public_url(url) == expected


@pytest.mark.parametrize("error, secrets", [
    ("SESSDATA=private; bili_jct=csrf", ["private", "csrf"]),
    ('{"token": "private token", "password": "private pass"}', ["private token", "private pass"]),
    ("Authorization: Bearer private bearer\nnext line", ["private", "bearer"]),
    ("Cookie: SESSDATA=private; bili_jct=csrf; arbitrary=credential", ["private", "csrf", "credential"]),
    ("proxy https://username:password@example.test/path?sign=private", ["username", "password", "private"]),
])
def test_error_sanitization_covers_headers_json_and_proxy_urls(error, secrets):
    cleaned = clean_error(error)
    for secret in secrets:
        assert secret not in cleaned
    assert len(clean_error("x" * 2000)) == 1500


def test_load_and_save_both_sanitize_sources_and_failure_details(tmp_path):
    store = ArtifactStore(tmp_path, "target")
    value = manifest(store)
    value.source_url = "https://user:private@example.test/watch?v=1&sign=private"
    value.index_error = "Authorization: Bearer private"
    value.artifacts = [Artifact(
        "media", "failed", source_url="https://example.test/media?token=private",
        error='{"password": "private"}',
    )]
    raw = asdict(value)
    put_raw(store, raw)
    loaded = store.load("video", "video-1")
    assert "private" not in json.dumps(asdict(loaded))
    assert loaded.source_url == "https://example.test/watch?v=1"
    store.save(value)
    assert "private" not in store.path_for("video", "video-1").read_text(encoding="utf-8")
