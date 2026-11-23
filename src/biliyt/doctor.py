"""Environment and configuration health checks."""

from __future__ import annotations

import importlib
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from .config import AppConfig, load_config, resolve_target


@dataclass(frozen=True)
class CheckItem:
    name: str
    status: str  # "ok" | "warn" | "error"
    detail: str


def _cmd_version(executable: str, args: list[str] | None = None) -> str | None:
    path = shutil.which(executable)
    if not path:
        return None
    try:
        proc = subprocess.run(
            [path, *(args or ["--version"])],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        line = (proc.stdout or proc.stderr or "").strip().splitlines()
        return f"{line[0]} ({path})" if line else path
    except Exception:
        return path


def _has_module(module_name: str) -> bool:
    try:
        importlib.import_module(module_name)
        return True
    except Exception:
        return False


def _test_proxy_tcp(proxy_url: str, timeout: float = 2.0) -> tuple[bool, str]:
    parsed = urlparse(proxy_url)
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return False, f"代理格式无效: {proxy_url}"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"TCP 连通 ({host}:{port})"
    except OSError as exc:
        return False, f"无法连接 {host}:{port} ({exc})"


def run_environment_checks(config_path: Path | None = None) -> list[CheckItem]:
    items: list[CheckItem] = []

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    items.append(
        CheckItem(
            "Python",
            "ok" if sys.version_info >= (3, 11) else "error",
            f"{py_ver} ({sys.executable})",
        )
    )

    items.append(CheckItem("yt-dlp", "ok", yt_dlp.version.__version__))

    ejs_ok = _has_module("yt_dlp_ejs")
    items.append(
        CheckItem(
            "yt-dlp-ejs",
            "ok" if ejs_ok else "warn",
            "已安装（YouTube 挑战脚本支持）" if ejs_ok else "未安装，YouTube 高画质解析可能受限",
        )
    )

    cffi_ok = _has_module("curl_cffi")
    items.append(
        CheckItem(
            "curl_cffi",
            "ok" if cffi_ok else "warn",
            "已安装（支持浏览器 TLS 指纹模拟）" if cffi_ok else "未安装，--impersonate 不可用",
        )
    )

    ffmpeg_ver = _cmd_version("ffmpeg", ["-version"])
    items.append(
        CheckItem(
            "FFmpeg",
            "ok" if ffmpeg_ver else "error",
            ffmpeg_ver or "未找到 ffmpeg，音视频混流与格式转换将失败",
        )
    )

    ffprobe_ver = _cmd_version("ffprobe", ["-version"])
    items.append(
        CheckItem(
            "FFprobe",
            "ok" if ffprobe_ver else "warn",
            ffprobe_ver or "未找到 ffprobe",
        )
    )

    deno_ver = _cmd_version("deno")
    node_ver = _cmd_version("node")
    if deno_ver:
        items.append(CheckItem("JS Runtime", "ok", f"Deno: {deno_ver}"))
    elif node_ver:
        items.append(CheckItem("JS Runtime", "ok", f"Node.js: {node_ver}（自动注入 YouTube EJS）"))
    else:
        items.append(
            CheckItem(
                "JS Runtime",
                "warn",
                "未检测到 Deno 或 Node.js，YouTube 高画质流可能缺失",
            )
        )

    if config_path is not None:
        if not config_path.exists():
            items.append(
                CheckItem(
                    "配置文件",
                    "warn",
                    f"未找到 {config_path}（可运行 `biliyt init` 生成模板）",
                )
            )
        else:
            try:
                cfg, base_dir = load_config(config_path)
                items.extend(_check_config(cfg, base_dir))
            except Exception as exc:
                items.append(CheckItem("配置文件", "error", f"校验失败: {exc}"))

    return items


def _check_config(cfg: AppConfig, base_dir: Path) -> list[CheckItem]:
    items: list[CheckItem] = []
    enabled_count = sum(1 for t in cfg.targets if t.enabled)
    items.append(
        CheckItem(
            "配置文件",
            "ok",
            f"校验通过：共 {len(cfg.targets)} 个目标（已启用 {enabled_count} 个）",
        )
    )

    checked_proxies: set[str] = set()
    checked_cookies: set[str] = set()

    for platform, pcfg in cfg.platforms.items():
        if pcfg.proxy and pcfg.proxy not in checked_proxies:
            checked_proxies.add(pcfg.proxy)
            ok, msg = _test_proxy_tcp(pcfg.proxy)
            items.append(CheckItem(f"{platform.value} 代理", "ok" if ok else "warn", msg))

    for target in cfg.targets:
        if not target.enabled:
            continue
        resolved = resolve_target(cfg, target, base_dir)
        if resolved.proxy and resolved.proxy not in checked_proxies:
            checked_proxies.add(resolved.proxy)
            ok, msg = _test_proxy_tcp(resolved.proxy)
            items.append(CheckItem(f"代理 ({resolved.id})", "ok" if ok else "warn", msg))
        if resolved.auth.cookies_file and resolved.auth.cookies_file not in checked_cookies:
            checked_cookies.add(resolved.auth.cookies_file)
            exists = Path(resolved.auth.cookies_file).exists()
            items.append(
                CheckItem(
                    f"Cookie 文件 ({resolved.id})",
                    "ok" if exists else "error",
                    resolved.auth.cookies_file if exists else f"文件不存在: {resolved.auth.cookies_file}",
                )
            )

    return items
