# 贡献指南（Contributing）

感谢你关注并参与 `biliyt` 的改进。

## 开发环境准备

前置依赖：Python `>=3.11` 与 [`uv`](https://docs.astral.sh/uv/)（如需端到端测试媒体混流，另需本机安装 `ffmpeg` / `ffprobe`）。

```bash
git clone https://github.com/chosenecho/biliyt-archiver.git
cd biliyt-archiver
uv sync
```

## 运行测试与代码检查

提交代码前请确保所有单测与 Lint 检查通过：

```bash
# 运行全部单元测试（全离线、不发真实网络请求）
uv run pytest -v

# 运行 Ruff 静态检查
uvx ruff check src tests
```

> [!NOTE]
> Windows 环境下，如果本机虚拟环境 `.venv\Scripts\python.exe` 出现 `_ssl` / `_ctypes` DLL 初始化异常，可用系统 Python 配合 `PYTHONPATH` 运行：
> ```powershell
> $env:PYTHONPATH = "$PWD\.venv\Lib\site-packages;$PWD\src"
> python -m pytest -v
> ```

## 代码与文档约定

- **配置同步**：在 `src/biliyt/config.py` 新增或修改字段时，请同步更新 [config.example.yaml](config.example.yaml) 与 [docs/configuration.md](docs/configuration.md)，并补充单测。
- **离线可测**：`tests/` 中的单元测试必须完全离线，通过 stub/mock 覆盖 `yt-dlp`、B 站 API 与文件系统，不得依赖外网或真实 Cookie。
- **SQLite 路径规范**：写入 `state/biliyt.db` 的本地文件路径必须统一使用相对 `download_dir` 的 POSIX 路径，保持归档目录可迁移、可离线重建。
- **隐私红线**：绝不提交 `config.yaml`、`cookies/`、`state/`、`downloads/` 或包含真实 `SESSDATA` / `bili_jct` 的测试夹具。

## 提交与 Pull Request

- 提交信息采用 [Conventional Commits](https://www.conventionalcommits.org/) 风格，例如：
  - `feat: add ...`
  - `fix: handle ...`
  - `refactor: simplify ...`
  - `docs: update ...`
  - `test: cover ...`
- 发起 PR 前：
  1. 保持单次 PR 聚焦一个问题或特性。
  2. 为新逻辑或修复的 Bug 补充对应单元测试。
  3. 确认 `pytest`、`ruff check` 与 `uv build` 均通过。

## 报告问题与提建议

- 提交 Bug 前请先查阅 [docs/troubleshooting.md](docs/troubleshooting.md)，并先尝试升级 `yt-dlp` 到最新版本。
- 使用仓库提供的 [Issue 模板](https://github.com/chosenecho/biliyt-archiver/issues/new/choose)，并附上脱敏后的 `biliyt check` 输出。
- 如涉及安全漏洞，请遵循 [SECURITY.md](SECURITY.md) 私密上报。
