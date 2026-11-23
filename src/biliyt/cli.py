"""Command-line interface for biliyt."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import AppConfig, ResolvedTarget, load_config, resolve_target
from .doctor import run_environment_checks
from .downloader import SyncEngine, build_run_report
from .platform import Platform
from .state import StateStore

app = typer.Typer(
    name="biliyt",
    help="基于 yt-dlp 的 Bilibili / YouTube 目标用户批量增量下载工作流",
    no_args_is_help=True,
)
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
    if template is None:
        console.print("[red]未找到模板文件 config.example.yaml[/red]")
        raise typer.Exit(code=1)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, target_path)
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

    console.print(summary_table)


@app.command()
def sync(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="配置文件路径"),
    target: Optional[list[str]] = typer.Option(None, "--target", "-t", help="仅同步指定 target id（可多次传入）"),
    platform: Optional[Platform] = typer.Option(None, "--platform", "-p", help="仅同步指定平台"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", min=1, help="限制每个目标本次最多检查/下载数量"),
    dry_run: bool = typer.Option(False, "--dry-run", help="演练模式：只列出将要下载的条目，不实际下载"),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="仅重试 state/failed.jsonl 中的历史失败条目"),
) -> None:
    """增量同步目标用户视频（支持 dry-run 与历史失败重试）。"""
    try:
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
            console.print(
                f"    发现 {len(probe_res.entries)} 条 | 已归档 {probe_res.archived_count} | "
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
        elif event == "item_done":
            outcome = payload["outcome"]
            detail = payload["detail"]
            if outcome == "downloaded":
                console.print("      [green]✓ 下载完成[/green]")
            elif outcome == "filtered":
                console.print(f"      [yellow]- 跳过: {detail}[/yellow]")
            else:
                console.print(f"      [red]✗ 失败: {detail}[/red]")
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

    engine = SyncEngine(state, on_event=_on_event)

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
            engine.sync_target(t, dry_run=dry_run, limit_override=limit) for t in targets
        ]
        mode = "sync_dry_run" if dry_run else "sync"

    report = build_run_report(mode, started_at, summaries)
    if not dry_run:
        report_path = state.save_report(report)
    else:
        report_path = None

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
    table.add_column("Target", style="cyan")
    table.add_column("平台")
    table.add_column("启用")
    table.add_column("累计归档", justify="right", style="green")
    table.add_column("待重试失败", justify="right", style="red")
    table.add_column("输出目录", style="dim")

    for t in targets:
        archived_n = len(state.read_archive_entries(t.id))
        failed_n = len(state.load_failed(t.id))
        table.add_row(
            t.id,
            t.platform.value,
            "是" if t.enabled else "[dim]否[/dim]",
            str(archived_n),
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
