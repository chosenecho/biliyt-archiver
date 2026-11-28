"""Offline checks for single-file atomic replacement and failure cleanup."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import pytest

from biliyt import atomic_io


def test_write_formats_and_parent_creation(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data"
    atomic_io.atomic_write_bytes(path, b"\x00\xff")
    assert path.read_bytes() == b"\x00\xff"
    atomic_io.atomic_write_text(path, "归档", encoding="utf-16")
    assert path.read_text(encoding="utf-16") == "归档"
    atomic_io.atomic_write_json(path, {"title": "归档"}, sort_keys=True)
    assert json.loads(path.read_text(encoding="utf-8")) == {"title": "归档"}
    assert "归档" in path.read_text(encoding="utf-8")
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("stage", ["write", "flush", "fsync", "replace"])
@pytest.mark.parametrize("existing", [False, True])
def test_failure_preserves_destination_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, existing: bool
) -> None:
    path = tmp_path / "data.txt"
    if existing:
        path.write_bytes(b"original")

    def fail(*args, **kwargs):
        raise OSError(f"injected {stage} failure")

    if stage in {"write", "flush"}:
        real_fdopen = atomic_io.os.fdopen

        class BrokenStream:
            def __init__(self, fd, mode):
                self.stream = real_fdopen(fd, mode)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def write(self, data):
                if stage == "write":
                    self.stream.write(data[:1])
                    fail()
                return self.stream.write(data)

            def flush(self):
                fail()

        monkeypatch.setattr(atomic_io.os, "fdopen", BrokenStream)
    else:
        monkeypatch.setattr(atomic_io.os, stage, fail)

    with pytest.raises(OSError, match=f"injected {stage}"):
        atomic_io.atomic_write_bytes(path, b"replacement")
    assert path.exists() is existing
    if existing:
        assert path.read_bytes() == b"original"
    assert set(tmp_path.iterdir()) == ({path} if existing else set())


def test_serialization_failure_preserves_original(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text("original", encoding="utf-8")
    with pytest.raises(TypeError):
        atomic_io.atomic_write_json(path, {"bad": object()})
    with pytest.raises(UnicodeEncodeError):
        atomic_io.atomic_write_text(path, "归档", encoding="ascii")
    assert path.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [path]


def test_fsync_precedes_closed_handle_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "data.txt"
    events = []
    real_fsync = atomic_io.os.fsync
    real_replace = atomic_io.os.replace

    def sync(fd):
        events.append("fsync")
        real_fsync(fd)

    def replace(source, destination):
        temporary = Path(source)
        assert temporary.parent == destination.parent
        assert temporary.read_text(encoding="utf-8") == "new"
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(atomic_io.os, "fsync", sync)
    monkeypatch.setattr(atomic_io.os, "replace", replace)
    atomic_io.atomic_write_text(path, "new")
    assert events == ["fsync", "replace"]


def test_concurrent_writers_use_unique_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "shared.txt"
    payloads = [str(i) * 10000 for i in range(20)]
    temporary_names = []
    replace_lock = Lock()
    real_replace = atomic_io.os.replace

    def replace(source, destination):
        # Windows may reject simultaneous renames to one destination. Serialize
        # only that OS operation; temporary creation/writes remain concurrent.
        with replace_lock:
            temporary_names.append(source)
            real_replace(source, destination)

    monkeypatch.setattr(atomic_io.os, "replace", replace)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda value: atomic_io.atomic_write_text(path, value), payloads))
    assert len(set(temporary_names)) == len(payloads)
    assert path.read_text(encoding="utf-8") in payloads
    assert list(tmp_path.iterdir()) == [path]
