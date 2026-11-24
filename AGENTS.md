# AGENTS.md

Coding-agent context for `biliyt` (`chosenecho/biliyt-archiver`), a Python 3.11+ CLI that incrementally archives Bilibili creators and YouTube channels via `yt-dlp`, Bilibili WBI Web APIs (`curl_cffi`), and local SQLite FTS5.

## Commands

- Install dependencies: `uv sync`
- Run full test suite (offline): `uv run pytest -v`
- Run a single test file: `uv run pytest tests/test_config.py -v`
- Lint: `uvx ruff check src tests` (config lives in `pyproject.toml`)
- Build wheel & sdist: `uv build`
- Run CLI locally: `uv run biliyt --help` (or `biliyt --help` when installed globally via `uv tool install`)

**Windows venv quirk**: On some Windows setups `.venv\Scripts\python.exe` fails to load `_ssl`/`_ctypes` DLLs. When that happens, run tests and CLI using system Python with site-packages on `PYTHONPATH`:

```powershell
$env:PYTHONPATH = "$PWD\.venv\Lib\site-packages;$PWD\src"
python -m pytest -v
```

## Architecture Map

```text
src/biliyt/
├── cli.py              # Typer + Rich entrypoint (init, check, probe, sync, status, search, db stats/rebuild)
├── config.py           # Pydantic v2 schema + global -> platform -> target partial override merge
├── platform.py         # Platform detection & URL normalization (UID / @handle / playlist / space)
├── options.py          # yt-dlp option builder, EJS js_runtime auto-detection, pre/post filters
├── downloader.py       # Two-phase SyncEngine (probe -> filter -> concurrent download + extended archive)
├── state.py            # Thread-safe StateStore (archives/*.txt, failed.jsonl, run reports)
├── db.py               # SQLite WAL + FTS5 trigram ArchiveDB & offline rebuild_target_from_disk()
├── doctor.py           # Environment, proxy TCP, cookie & config health checks
└── bilibili/
    ├── wbi.py          # Bilibili WBI dynamic request signing
    ├── client.py       # curl_cffi HTTP client, Netscape CookieJar, throttle, -352/-412 circuit breaker
    ├── render.py       # Markdown + local image renderer (dynamics, cv/Opus articles, favorites, comments)
    └── archiver.py     # Extended Bilibili module orchestration (profile, dynamics, articles, favorites, comments)
```

## Context Pointers

Read the matching doc only when your task touches that branch:

- Changing `config.py` fields or override rules → read `docs/configuration.md` and `config.example.yaml` and keep all three in sync.
- Changing Bilibili extended archiving, WBI signing, or rate-limit circuit breaking → read `docs/bilibili-full-archive-pdr.md` and `docs/bilibili-full-archive-sop.md`.
- Changing SQLite tables, FTS5 indexing, or `db rebuild` / `search` → read `docs/sqlite-storage-sop.md`.
- Debugging yt-dlp / YouTube EJS / Bilibili 412 / Windows cookie issues → read `docs/troubleshooting.md`.

## Invariants & Conventions

- **Three-layer config merge**: `resolve_target()` in `src/biliyt/config.py` layers `global -> platform -> target` using `model_fields_set`. Preserve unset-field inheritance when adding options; never overwrite inherited values with defaults.
- **Thread-safe state**: `downloader.py` runs concurrent video workers (`rate_limit.concurrency` 1–8). Any new `StateStore` write path must hold the internal lock in `src/biliyt/state.py`.
- **Hybrid storage boundary**: Store media binaries, `.md`, `.srt`, and raw `.json` under `downloads/`. Store structured metadata and FTS5 indexes in `state/biliyt.db` using POSIX paths relative to `download_dir` so archives stay portable and `biliyt db rebuild` remains lossless.
- **Schema migrations**: Never mutate existing SQLite tables in-place without adding an idempotent migration version in `schema_migrations` (`src/biliyt/db.py`).
- **Packaged config template**: `pyproject.toml` force-includes root `config.example.yaml` into the wheel as `biliyt/config.example.yaml` so `biliyt init` works outside a git checkout. Keep `config.example.yaml` valid YAML that passes `load_config()`.
- **Offline tests only**: Keep every test in `tests/` 100% offline with mocks/stubs and `tmp_path`. Add or update tests alongside any behavior change.

## Security Boundaries

- Keep runtime/secret files untracked: `config.yaml`, `cookies/`, `state/`, `downloads/`, `*.cookies.txt`, `cookies.txt`.
- Never log, echo into reports, or commit real cookies (`SESSDATA`, `bili_jct`, `DedeUserID`, `buvid3`) or proxy credentials.
- Never hardcode machine-specific absolute paths in code, docs, or tests.

## Pull Request Gate

Run before finishing any change:
1. `uv run pytest -v` (all tests green)
2. `uvx ruff check src tests` (zero warnings)
3. Verify `config.example.yaml` and `docs/configuration.md` match any schema changes.
4. Use Conventional Commits (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`).
