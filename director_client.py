# -*- coding: utf-8 -*-
"""H3 导演台 HTTP 客户端（仅标准库，无第三方依赖）。

只调用导演台公开的只读接口：
  GET /status
不修改导演台、不接管 ComfyUI 队列、不启动任何进程。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import quote

from .config import load_config


class DirectorError(RuntimeError):
    pass


def _get_json(path: str, url: str = "", timeout: float | None = None) -> dict:
    cfg = load_config({"director_url": url} if url else None)
    base = cfg["director_url"].rstrip("/")
    req = urllib.request.Request(base + path, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or cfg["timeout"]) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        raise DirectorError(f"导演台返回 HTTP {exc.code}：{base}{path}") from exc
    except urllib.error.URLError as exc:
        raise DirectorError(
            f"连不上导演台 {base}（{exc.reason}）。请先启动 H3 导演台服务。") from exc
    except TimeoutError as exc:
        raise DirectorError(f"导演台响应超时：{base}{path}") from exc
    try:
        obj = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise DirectorError(f"导演台返回了非 JSON 内容：{base}{path}") from exc
    if not isinstance(obj, dict):
        raise DirectorError("导演台 /status 结构异常（不是对象）")
    return obj


def get_status(url: str = "", timeout: float | None = None) -> dict:
    return _get_json("/status", url=url, timeout=timeout)


def summarize_status(data: dict) -> str:
    """把 /status 压成一行人类可读摘要，给节点输出。"""
    overall = data.get("overall") or {}
    tasks = data.get("tasks") or []
    films = data.get("films") or []
    running = [t for t in tasks if str(t.get("state")) in ("running", "queued", "pending")]
    phase = overall.get("text") or overall.get("phase") or "unknown"
    bits = [f"状态: {phase}", f"任务: {len(tasks)}（进行中 {len(running)}）",
            f"长片: {len(films)}"]
    cur = running[0] if running else None
    if cur:
        bits.append("当前: " + str(cur.get("name") or cur.get("file") or "?"))
        if cur.get("stage"):
            bits.append("阶段: " + str(cur["stage"]))
    return " | ".join(bits)


def get_last_result(data: dict) -> dict:
    """导演台最近一次交付结果（可能为空对象）。"""
    lr = data.get("last_result")
    return lr if isinstance(lr, dict) else {}


def download_url(name: str, url: str = "") -> str:
    cfg = load_config({"director_url": url} if url else None)
    return f"{cfg['director_url'].rstrip('/')}/download?name={quote(str(name))}"
