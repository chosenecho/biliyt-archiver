"""Command-line interface for biliyt."""

from __future__ import annotations

import shutil
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
from importlib import resources as importlib_resources
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import AppConfig, ResolvedTarget, load_config, resolve_target
from .db import ArchiveDB, rebuild_target_from_disk
from .doctor import run_environment_checks
from .downloader import SyncEngine, build_run_report
from .platform import Platform
from .state import StateStore, SyncLockError

app = typer.Typer(
    name="biliyt",
    help="基于 yt-dlp 的 Bilibili / YouTube 目标用户批量增量下载与归档工作流",
    no_args_is_help=True,
)
db_app = typer.Typer(
    name="db",
    help="SQLite 归档数据库统计与离线重建工具",
    no_args_is_help=True,
)
app.add_typer(db_app, name="db")
console = Console()


def _load_and_select_targets(
    config_path: Path,
    target_ids: list[str] | None = None,
    platform: Platform | None = None,
    include_disabled: bool = False,
) -> tuple[AppConfig, Path, list[ResolvedTarget]]:
    cfg, base_dir = load_config(config_path)
    selected: list[ResolvedTarget] = []
    wanted_ids = set(target_ids or [])

    for t in cfg.targets:
        if not include_disabled and not t.enabled and t.id not in wanted_ids:
            continue
        if wanted_ids and t.id not in wanted_ids:
            continue
        if platform is not None and t.platform != platform:
            continue
        selected.append(resolve_target(cfg, t, base_dir))

    if wanted_ids:
        found = {t.id for t in selected}
        missing = sorted(wanted_ids - found)
        if missing:
            raise typer.BadParameter(f"配置中未找到以下 target id: {', '.join(missing)}")

    return cfg, base_dir, selected


@app.command()
def init(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="要生成的配置文件路径"),
    force: bool = typer.Option(False, "--force", "-f", help="覆盖已存在的配置文件"),
) -> None:
    """生成模板配置文件 config.yaml 与运行目录。"""
    target_path = config.expanduser().resolve()
    if target_path.exists() and not force:
        console.print(f"[yellow]配置文件已存在:[/yellow] {target_path}（如需覆盖请加 --force）")
        raise typer.Exit(code=1)

    template_candidates = [
        Path.cwd() / "config.example.yaml",
        Path(__file__).resolve().parents[2] / "config.example.yaml",
    ]
    template = next((p for p in template_candidates if p.exists()), None)
    bundled_template = importlib_resources.files("biliyt").joinpath("config.example.yaml")
    if template is None and not bundled_template.is_file():
        console.print("[red]未找到模板文件 config.example.yaml[/red]")
        raise typer.Exit(code=1)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if template is not None:
        shutil.copyfile(template, target_path)
    else:
        target_path.write_text(bundled_template.read_text(encoding="utf-8"), encoding="utf-8")
    (target_path.parent / "cookies").mkdir(exist_ok=True)

    console.print(
        Panel.fit(
            f"已生成配置文件: [green]{target_path}[/green]\n"
            "下一步:\n"
            "1. 编辑 config.yaml，填入你的 B 站/YouTube 目标并将 enabled 改为 true\n"
            "2. 运行 [bold]biliyt check[/bold] 检查依赖、代理与 Cookie\n"
            "3. 运行 [bold]biliyt probe[/bold] 预览待下载视频，再执行 [bold]biliyt sync[/bold]",
            title="初始化完成",
        )
    )


@app.command()
def check(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="配置文件路径"),
) -> None:
    """检查运行依赖（yt-dlp / FFmpeg / JS Runtime / curl_cffi）与配置有效性。"""
    items = run_environment_checks(config.expanduser().resolve())
    table = Table(title=f"biliyt v{__version__} 环境与配置体检")
    table.add_column("检查项", style="cyan", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("详情")

    status_styles = {
        "ok": "[green]OK[/green]",
        "warn": "[yellow]WARN[/yellow]",
        "error": "[red]ERROR[/red]",
    }
    has_error = False
    for item in items:
        if item.status == "error":
            has_error = True
        table.add_row(item.name, status_styles.get(item.status, item.status), item.detail)

    console.print(table)
    if has_error:
        raise typer.Exit(code=1)


@app.command()
def probe(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="配置文件路径"),
    target: Optional[list[str]] = typer.Option(None, "--target", "-t", help="仅探查指定 target id（可多次传入）"),
    platform: Optional[Platform] = typer.Option(None, "--platform", "-p", help="仅探查指定平台"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", min=1, help="临时覆盖 recent_limit"),
    show_items: int = typer.Option(10, "--show", min=0, help="每个目标最多展示前 N 条待下载明细"),
) -> None:
    """探查目标用户投稿列表，统计已归档/被过滤/待下载数量（不下载）。"""
    try:
        cfg, base_dir, targets = _load_and_select_targets(config, target, platform)
    except Exception as exc:
        console.print(f"[red]加载配置失败:[/red] {exc}")
        raise typer.Exit(code=1)

    if not targets:
        console.print("[yellow]没有启用的目标。请检查 config.yaml 中的 targets 与 enabled 字段。[/yellow]")
        return

    state = StateStore(targets[0].state_dir)
    engine = SyncEngine(state)

    summary_table = Table(title="目标探查结果")
    summary_table.add_column("Target", style="cyan")
    summary_table.add_column("平台")
    summary_table.add_column("发现", justify="right")
    summary_table.add_column("已归档", justify="right", style="dim")
    summary_table.add_column("过滤跳过", justify="right", style="yellow")
    summary_table.add_column("待下载", justify="right", style="green")
    summary_table.add_column("状态")

    for t in targets:
        console.print(f"正在探查 [bold cyan]{t.name}[/bold cyan] ({t.url}) ...")
        res = engine.probe_target(t, limit_override=limit)
        if res.error:
            summary_table.add_row(t.id, t.platform.value, "-", "-", "-", "-", f"[red]{res.error}[/red]")
            continue

        summary_table.add_row(
            t.id,
            t.platform.value,
            str(len(res.entries)),
            str(res.archived_count),
            str(res.filtered_count),
            str(len(res.pending)),
            "[green]OK[/green]",
        )

        if show_items > 0 and res.pending:
            detail_table = Table(title=f"待下载预览: {t.name}（前 {min(show_items, len(res.pending))} 条）")
            detail_table.add_column("#", justify="right", style="dim")
            detail_table.add_column("ID", style="cyan")
            detail_table.add_column("日期")
            detail_table.add_column("标题")
            for idx, entry in enumerate(res.pending[:show_items], start=1):
                detail_table.add_row(
                    str(idx),
                    entry.video_id or "-",
                    entry.upload_date or "-",
                    entry.title,
                )
            console.print(detail_table)

        if t.platform == Platform.BILIBILI and t.archive.any_extended_enabled():
            ext = engine.probe_extended_bilibili(t, limit_override=limit)
            if ext.error:
                console.print(f"    [yellow]扩展内容探查提示:[/yellow] {ext.error}")
            else:
                console.print(
                    f"    [cyan]B 站扩展探查[/cyan] (UID={ext.uid}): "
                    f"动态抽样 {ext.dynamics_sampled} (待归档 {ext.dynamics_pending}) | "
                    f"专栏总计 {ext.articles_total} (首页待归档 {ext.articles_pending}) | "
                    f"公开收藏夹 {ext.favorites_folders} 个"
                )

    console.print(summary_table)


_VALID_ONLY_MODULES = {"profile", "videos", "dynamics", "articles", "favorites", "comments"}


def _parse_only_modules(raw: str | None) -> set[str] | None:
    if not raw or not raw.strip():
        return None
    mods = {part.strip().lower() for part in raw.split(",") if part.strip()}
    unknown = sorted(mods - _VALID_ONLY_MODULES)
    if unknown:
        raise typer.BadParameter(
            f"未知模块: {', '.join(unknown)}。可选值: {', '.join(sorted(_VALID_ONLY_MODULES))}"
        )
    return mods


@app.command()
def sync(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="配置文件路径"),
    target: Optional[list[str]] = typer.Option(None, "--target", "-t", help="仅同步指定 target id（可多次传入）"),
    platform: Optional[Platform] = typer.Option(None, "--platform", "-p", help="仅同步指定平台"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", min=1, help="限制每个目标本次最多新下载/新保存数量（可与 --full 联用分批回溯历史）"),
    only: Optional[str] = typer.Option(
        None,
        "--only",
        help="仅运行指定模块，逗号分隔（profile,videos,dynamics,articles,favorites,comments）",
    ),
    full: bool = typer.Option(False, "--full", help="全量回溯模式：忽略扫描上限与增量早停（可配合 --limit 分批补齐）"),
    refresh_comments: bool = typer.Option(False, "--refresh-comments", help="强制刷新已归档条目的精选评论"),
    dry_run: bool = typer.Option(False, "--dry-run", help="演练模式：只列出将要下载的条目，不实际下载"),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="仅重试 state/failed.jsonl 中的历史失败条目"),
) -> None:
    """增量同步目标用户视频与 B 站全量内容（支持 dry-run、分模块与历史失败重试）。"""
    try:
        only_modules = _parse_only_modules(only)
        cfg, base_dir, targets = _load_and_select_targets(
            config, target, platform, include_disabled=retry_failed and bool(target)
        )
    except Exception as exc:
        console.print(f"[red]加载配置失败:[/red] {exc}")
        raise typer.Exit(code=1)

    if not targets:
        console.print("[yellow]没有可同步的目标。请检查 config.yaml 中的 targets 与 enabled 字段。[/yellow]")
        return

    state = StateStore(targets[0].state_dir)
    started_at = datetime.now(timezone.utc)

    def _on_event(event: str, payload: dict[str, Any]) -> None:
        if event == "probe_start":
            t: ResolvedTarget = payload["target"]
            console.print(f"\n[bold cyan]>>> [{t.platform.value}] {t.name}[/bold cyan] ({t.url})")
        elif event == "probe_done":
            probe_res = payload["probe"]
            stop_tag = " [dim](增量早停)[/dim]" if getattr(probe_res, "early_stopped", False) else ""
            console.print(
                f"    发现 {len(probe_res.entries)} 条{stop_tag} | 已归档 {probe_res.archived_count} | "
                f"过滤跳过 {probe_res.filtered_count} | 待下载 [green]{len(probe_res.pending)}[/green]"
            )
            if dry_run and probe_res.pending:
                for idx, entry in enumerate(probe_res.pending, start=1):
                    console.print(f"    [dim][dry-run {idx}][/dim] {entry.title} ({entry.url})")
        elif event == "item_start":
            item = payload["item"]
            console.print(
                f"    [{payload['index']}/{payload['total']}] 下载: [bold]{item.title}[/bold]"
            )
        elif event == "item_warning":
            console.print(f"      [yellow]! {rich_escape(str(payload['message']))}[/yellow]")
        elif event == "item_done":
            outcome = payload["outcome"]
            detail = payload["detail"]
            item = payload.get("item")
            label = f" [{payload['index']}/{payload['total']}] {item.title}" if item else ""
            if outcome == "downloaded":
                console.print(f"      [green]✓ 下载完成[/green]{label}")
            elif outcome == "filtered":
                console.print(f"      [yellow]- 跳过: {detail}[/yellow]{label}")
            else:
                console.print(f"      [red]✗ 失败: {detail}[/red]{label}")
        elif event == "retry_start":
            rec = payload["record"]
            console.print(
                f"    [重试 {payload['index']}/{payload['total']}] {rec.title or rec.url} (第 {rec.attempts + 1} 次)"
            )
        elif event == "retry_done":
            outcome = payload["outcome"]
            if outcome == "downloaded":
                console.print("      [green]✓ 重试成功，已移出失败队列[/green]")
            else:
                console.print(f"      [red]✗ 重试仍失败: {payload['detail']}[/red]")
        elif event == "target_error":
            console.print(f"    [red]✗ 目标解析失败: {payload['error']}[/red]")
        elif event == "ext_module_start":
            console.print(f"    [cyan]• 同步模块 \\[{payload['module']}][/cyan] ...")
        elif event == "ext_module_done":
            st = payload["stats"]
            console.print(
                f"      发现 {st.discovered} | 已归档 {st.already_archived} | "
                f"新增 [green]{st.saved}[/green] | 失败 [red]{st.failed}[/red]"
            )
            for err in st.errors[:3]:
                console.print(f"      [red]! {err}[/red]")

    engine = SyncEngine(state, on_event=_on_event)

    lock_ctx = nullcontext() if dry_run else state.acquire_sync_lock()
    try:
        with lock_ctx:
            if retry_failed:
                targets_by_id = {t.id: t for t in targets}
                summaries = engine.retry_failed(
                    targets_by_id,
                    target_ids=set(target) if target else None,
                    dry_run=dry_run,
                )
                mode = "retry_failed_dry_run" if dry_run else "retry_failed"
            else:
                summaries = [
                    engine.sync_target(
                        t,
                        dry_run=dry_run,
                        limit_override=limit,
                        only=only_modules,
                        full=full,
                        refresh_comments=refresh_comments,
                    )
                    for t in targets
                ]
                mode = "sync_dry_run" if dry_run else "sync"

            report = build_run_report(mode, started_at, summaries)
            if not dry_run:
                report_path = state.save_report(report)
                try:
                    with ArchiveDB(state.state_dir / "biliyt.db") as db:
                        db.record_sync_run(
                            mode=report.mode,
                            started_at=report.started_at,
                            finished_at=report.finished_at,
                            summary_dict=asdict(report),
                        )
                except Exception:
                    pass
            else:
                report_path = None
    except SyncLockError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=3)

    table = Table(title="本次同步汇总" + ("（Dry-Run）" if dry_run else ""))
    table.add_column("Target", style="cyan")
    table.add_column("平台")
    table.add_column("发现", justify="right")
    table.add_column("已归档", justify="right", style="dim")
    table.add_column("跳过", justify="right", style="yellow")
    table.add_column("新下载", justify="right", style="green")
    table.add_column("失败", justify="right", style="red")

    total_failed = 0
    for s in summaries:
        total_failed += s.failed
        table.add_row(
            s.target_id,
            s.platform,
            str(s.discovered),
            str(s.already_archived),
            str(s.filtered_out),
            str(s.downloaded),
            str(s.failed),
        )

    console.print()
    console.print(table)

    has_module_breakdown = any(len(s.module_stats) > 1 or (s.module_stats and "videos" not in s.module_stats) for s in summaries)
    if has_module_breakdown:
        mod_table = Table(title="分模块同步明细")
        mod_table.add_column("Target", style="cyan")
        mod_table.add_column("模块")
        mod_table.add_column("发现", justify="right")
        mod_table.add_column("已归档", justify="right", style="dim")
        mod_table.add_column("新增保存", justify="right", style="green")
        mod_table.add_column("失败", justify="right", style="red")
        for s in summaries:
            for mod_name, ms in s.module_stats.items():
                mod_table.add_row(
                    s.target_id,
                    mod_name,
                    str(ms.get("discovered", 0)),
                    str(ms.get("already_archived", 0)),
                    str(ms.get("saved", 0)),
                    str(ms.get("failed", 0)),
                )
        console.print(mod_table)

    if report_path:
        console.print(f"[dim]运行报告已保存: {report_path}[/dim]")
    if total_failed > 0:
        console.print("[yellow]提示: 失败条目已记入 state/failed.jsonl，可用 `biliyt sync --retry-failed` 重试。[/yellow]")
        raise typer.Exit(code=2)


@app.command()
def status(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="配置文件路径"),
) -> None:
    """查看各目标的累计归档数、失败队列与最近一次运行摘要。"""
    try:
        cfg, base_dir, targets = _load_and_select_targets(config, include_disabled=True)
    except Exception as exc:
        console.print(f"[red]加载配置失败:[/red] {exc}")
        raise typer.Exit(code=1)

    state_dir = (
        targets[0].state_dir
        if targets
        else (base_dir / cfg.paths.state_dir).resolve()
    )
    state = StateStore(state_dir)

    table = Table(title="工作流状态总览")
    table.add_column("Target", style="cyan", no_wrap=True)
    table.add_column("平台", no_wrap=True)
    table.add_column("启用", no_wrap=True)
    table.add_column("视频", justify="right", style="green", no_wrap=True)
    table.add_column("动/专/评", justify="right", style="green", no_wrap=True)
    table.add_column("失败", justify="right", style="red", no_wrap=True)
    table.add_column("输出目录", style="dim")

    for t in targets:
        archived_n = len(state.read_archive_entries(t.id))
        dyn_n = state.count_namespace_archive(t.id, "dynamics")
        art_n = state.count_namespace_archive(t.id, "articles")
        cmt_n = state.count_namespace_archive(t.id, "comments")
        failed_n = len(state.load_failed(t.id))
        table.add_row(
            t.id,
            t.platform.value,
            "是" if t.enabled else "[dim]否[/dim]",
            str(archived_n),
            f"{dyn_n}/{art_n}/{cmt_n}",
            str(failed_n),
            str(t.download_dir),
        )

    console.print(table)

    last = state.load_last_report()
    if last:
        console.print(
            f"[dim]最近运行: mode={last.get('mode')} | "
            f"started={last.get('started_at')} | finished={last.get('finished_at')}[/dim]"
        )
    else:
        console.print("[dim]尚无同步运行记录。[/dim]")


def _resolve_state_dir(config: Path) -> tuple[AppConfig, Path, Path]:
    cfg, base_dir = load_config(config)
    raw = Path(cfg.paths.state_dir).expanduser()
    state_dir = raw if raw.is_absolute() else (base_dir / raw).resolve()
    return cfg, base_dir, state_dir


@db_app.command("stats")
def db_stats(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="配置文件路径"),
) -> None:
    """查看 SQLite 归档数据库体积与各表统计行数。"""
    try:
        _, _, state_dir = _resolve_state_dir(config)
    except Exception as exc:
        console.print(f"[red]加载配置失败:[/red] {exc}")
        raise typer.Exit(code=1)

    db_path = state_dir / "biliyt.db"
    if not db_path.exists():
        console.print(f"[yellow]数据库尚未创建:[/yellow] {db_path}（可运行 `biliyt db rebuild` 从本地文件重建）")
        return

    with ArchiveDB(db_path) as db:
        info = db.stats()

    size_kb = info["size_bytes"] / 1024
    table = Table(title=f"SQLite 归档统计 ({size_kb:.1f} KB)")
    table.add_column("数据表", style="cyan")
    table.add_column("记录数", justify="right", style="green")

    labels = {
        "profiles": "profiles (UP 主档案)",
        "profile_snapshots": "profile_snapshots (历史快照)",
        "videos": "videos (视频与字幕)",
        "dynamics": "dynamics (动态)",
        "articles": "articles (专栏)",
        "comments": "comments (精选评论)",
        "favorites": "favorites (公开收藏夹)",
        "favorite_items": "favorite_items (收藏视频清单)",
        "sync_runs": "sync_runs (同步日志)",
        "search_fts": "search_fts (全文检索索引项)",
    }
    for key, label in labels.items():
        table.add_row(label, str(info["counts"].get(key, 0)))

    console.print(table)
    console.print(f"[dim]数据库路径: {info['db_path']}[/dim]")


@db_app.command("rebuild")
def db_rebuild(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="配置文件路径"),
    target: Optional[list[str]] = typer.Option(None, "--target", "-t", help="仅重建指定 target id（可多次传入）"),
    reset: bool = typer.Option(False, "--reset", help="先清空现有数据库再从 downloads/ 全量重建"),
) -> None:
    """从本地 downloads/ 已有文件无损重建 SQLite 归档与全文索引（零网络请求）。"""
    try:
        cfg, base_dir, targets = _load_and_select_targets(
            config, target_ids=target, include_disabled=True
        )
    except Exception as exc:
        console.print(f"[red]加载配置失败:[/red] {exc}")
        raise typer.Exit(code=1)

    state_dir = (
        targets[0].state_dir
        if targets
        else (base_dir / cfg.paths.state_dir).resolve()
    )
    db_path = state_dir / "biliyt.db"

    table = Table(title="SQLite 离线重建汇总")
    table.add_column("Target", style="cyan", no_wrap=True)
    table.add_column("档案", justify="right")
    table.add_column("视频", justify="right", style="green")
    table.add_column("动态", justify="right", style="green")
    table.add_column("专栏", justify="right", style="green")
    table.add_column("收藏夹", justify="right")
    table.add_column("评论", justify="right", style="green")

    with ArchiveDB(db_path) as db:
        if reset:
            db.reset()
        for t in targets:
            c = rebuild_target_from_disk(db, t)
            table.add_row(
                t.id,
                str(c["profiles"]),
                str(c["videos"]),
                str(c["dynamics"]),
                str(c["articles"]),
                str(c["favorites"]),
                str(c["comments"]),
            )
        st = db.stats()

    console.print(table)
    console.print(
        f"[dim]数据库: {st['db_path']} ({st['size_bytes'] / 1024:.1f} KB) | "
        f"全文索引条目: {st['counts'].get('search_fts', 0)}[/dim]"
    )


_VALID_SEARCH_KINDS = {"video", "subtitle", "dynamic", "article", "comment"}


@app.command()
def search(
    query: str = typer.Argument(..., help="检索关键词（支持中文子串与空格分隔多词）"),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="配置文件路径"),
    target: Optional[str] = typer.Option(None, "--target", "-t", help="仅搜索指定 target id"),
    kind: Optional[str] = typer.Option(
        None,
        "--kind",
        "-k",
        help="按内容类型过滤，逗号分隔（video,subtitle,dynamic,article,comment）",
    ),
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200, help="最多返回条数"),
) -> None:
    """在本地 SQLite 归档库中跨视频、字幕、动态、专栏与评论全文检索。"""
    try:
        _, _, state_dir = _resolve_state_dir(config)
    except Exception as exc:
        console.print(f"[red]加载配置失败:[/red] {exc}")
        raise typer.Exit(code=1)

    kinds: set[str] | None = None
    if kind and kind.strip():
        kinds = {p.strip().lower() for p in kind.split(",") if p.strip()}
        unknown = sorted(kinds - _VALID_SEARCH_KINDS)
        if unknown:
            console.print(
                f"[red]未知类型:[/red] {', '.join(unknown)}（可选: {', '.join(sorted(_VALID_SEARCH_KINDS))}）"
            )
            raise typer.Exit(code=1)

    db_path = state_dir / "biliyt.db"
    if not db_path.exists():
        console.print(f"[yellow]尚未建立索引数据库:[/yellow] {db_path}，请先运行 `biliyt db rebuild`。")
        raise typer.Exit(code=1)

    with ArchiveDB(db_path) as db:
        hits = db.search(query, target_id=target, kinds=kinds, limit=limit)

    if not hits:
        console.print(f"[yellow]未找到与 {query!r} 匹配的本地归档内容。[/yellow]")
        return

    table = Table(title=f"本地归档检索结果：{query}（共 {len(hits)} 条）")
    table.add_column("类型", style="cyan", no_wrap=True)
    table.add_column("日期", no_wrap=True)
    table.add_column("标题 / 来源", style="bold")
    table.add_column("命中片段")
    table.add_column("链接", style="dim")

    for h in hits:
        table.add_row(
            h.kind,
            h.published_at[:10],
            rich_escape(h.title),
            rich_escape(h.snippet),
            rich_escape(h.source_url),
        )

    console.print(table)
