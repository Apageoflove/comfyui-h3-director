# -*- coding: utf-8 -*-
"""ComfyUI 节点：H3 导演台状态 / 提交长片 / 最近交付。

设计约束（零侵入）：
- 不 import folder_paths，不碰 ComfyUI 内部状态；
- 只读导演台公开 HTTP 接口，或向用户配置的 raw_dir 原子写 film.json；
- 所有异常转成清晰的 RuntimeError，绝不让 ComfyUI 崩溃。
"""
from __future__ import annotations

import json

from .version import __version__
from .config import ConfigError, load_config, write_film_json
from .director_client import (
    DirectorError,
    download_url,
    get_last_result,
    get_status,
    summarize_status,
)


def _status(url: str = "") -> dict:
    try:
        return get_status(url=url)
    except DirectorError as exc:
        raise RuntimeError(str(exc)) from exc


class H3DirectorStatus:
    """读取导演台 /status，输出一行摘要和原始 JSON。"""

    CATEGORY = "H3/Director"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("summary", "status_json")
    OUTPUT_NODE = True
    DESCRIPTION = "读取 H3 导演台 /status（只读，不改变任何任务）。"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}, "optional": {"director_url": ("STRING", {"default": ""})}}

    def run(self, director_url=""):
        data = _status(director_url)
        return (summarize_status(data), json.dumps(data, ensure_ascii=False))


class H3DirectorLastResult:
    """最近一次交付：成片、质检、字体、配乐。"""

    CATEGORY = "H3/Director"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("file", "verdict", "font", "bgm", "mood", "download_url", "raw_json")
    OUTPUT_NODE = True
    DESCRIPTION = "读取导演台最近一次交付结果（只读）。"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}, "optional": {"director_url": ("STRING", {"default": ""})}}

    def run(self, director_url=""):
        data = _status(director_url)
        r = get_last_result(data)
        file = str(r.get("file") or "")
        return (
            file,
            str(r.get("verdict") or ""),
            str(r.get("font") or ""),
            str(r.get("bgm") or ""),
            str(r.get("mood") or ""),
            download_url(file, director_url) if file else "",
            json.dumps(r, ensure_ascii=False),
        )


class H3DirectorSubmitFilm:
    """把剧本写成 <片名>.film.json 放进导演台监听的 raw_dir。"""

    CATEGORY = "H3/Director"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("film_json", "message")
    DESCRIPTION = ("生成 H3 导演台的长片任务文件（style/lang/lines/reuse），"
                   "原子落盘到 raw_dir，由导演台自动发现。")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "film_name": ("STRING", {"default": "my_film"}),
                "style": ("STRING", {"multiline": True, "default": "3D 卡通都市，克制、温柔"}),
                "lines": ("STRING", {"multiline": True, "default": "第一句台词\n第二句台词\n第三句台词"}),
            },
            "optional": {
                "lang": ("STRING", {"default": "zh"}),
                "reuse": ("STRING", {"multiline": True, "default": ""}),
                "raw_dir": ("STRING", {"default": ""}),
                "overwrite": ("BOOLEAN", {"default": False}),
                "director_url": ("STRING", {"default": ""}),
            },
        }

    def run(self, film_name, style, lines, lang="zh", reuse="", raw_dir="",
            overwrite=False, director_url=""):
        cfg = load_config({"director_url": director_url} if director_url else None)
        try:
            path = write_film_json(
                name=film_name,
                style=style,
                lang=lang,
                lines_text=lines,
                reuse_text=reuse,
                raw_dir=raw_dir or cfg["raw_dir"] or None,
                overwrite=bool(overwrite),
            )
        except ConfigError as exc:
            raise RuntimeError(f"H3 导演台提交失败：{exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"写入 film.json 失败：{exc}") from exc
        n = len([x for x in str(lines).splitlines() if x.strip()])
        msg = f"已写入 {path.name}（{n} 行台词，约 {(n + 2) // 3} 段）；导演台会在数秒内接手。"
        if director_url:
            msg += f" 导演台：{director_url.rstrip('/')}"
        return (str(path), msg)


NODE_CLASS_MAPPINGS = {
    "H3DirectorStatus": H3DirectorStatus,
    "H3DirectorLastResult": H3DirectorLastResult,
    "H3DirectorSubmitFilm": H3DirectorSubmitFilm,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "H3DirectorStatus": "H3 导演台 · 状态",
    "H3DirectorLastResult": "H3 导演台 · 最近交付",
    "H3DirectorSubmitFilm": "H3 导演台 · 提交长片",
}
PLUGIN_VERSION = __version__
