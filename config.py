# -*- coding: utf-8 -*-
"""插件本地配置：只读插件目录的 config.json + 环境变量覆盖。

不读取也不写入 ComfyUI 的任何目录；raw_dir 由用户显式配置。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
CONFIG_FILE = PLUGIN_DIR / "config.json"
EXAMPLE_FILE = PLUGIN_DIR / "config.example.json"

DEFAULTS = {
    "director_url": "http://127.0.0.1:8898",
    "raw_dir": "",
    "timeout": 8,
}

_SAFE_NAME = re.compile(r"^[^\\/:*?\"<>|\r\n]{1,120}$")


class ConfigError(ValueError):
    pass


def load_config(overrides: dict | None = None) -> dict:
    cfg = dict(DEFAULTS)
    for path in (CONFIG_FILE,):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in cfg})
        except FileNotFoundError:
            pass
        except Exception:
            # 配置坏了不能让 ComfyUI 起不来：退回默认值
            pass
    env_map = {
        "H3_DIRECTOR_URL": "director_url",
        "H3_DIRECTOR_RAW_DIR": "raw_dir",
        "H3_DIRECTOR_TIMEOUT": "timeout",
    }
    for env, key in env_map.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if k in cfg and v not in (None, "")})
    try:
        cfg["timeout"] = max(1.0, min(60.0, float(cfg["timeout"])))
    except Exception:
        cfg["timeout"] = float(DEFAULTS["timeout"])
    cfg["director_url"] = str(cfg["director_url"]).rstrip("/")
    return cfg


def resolve_raw_dir(cfg: dict) -> Path:
    raw = str(cfg.get("raw_dir") or "").strip()
    if not raw:
        raise ConfigError(
            "raw_dir 未配置：请在插件 config.json 里填写导演台监听的目录，"
            "或设置环境变量 H3_DIRECTOR_RAW_DIR")
    p = Path(raw)
    if not p.is_dir():
        raise ConfigError(f"raw_dir 不存在或不可访问：{p}")
    return p


def safe_film_name(name: str) -> str:
    name = (name or "").strip()
    if not _SAFE_NAME.match(name):
        raise ConfigError("片名不能为空，且不能包含 \\ / : * ? \" < > | 或换行")
    return name


def split_lines(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            out.append(line)
    return out


def write_film_json(name: str, style: str, lang: str, lines_text: str,
                    reuse_text: str = "", raw_dir: str | Path | None = None,
                    overwrite: bool = False) -> Path:
    """原子写入 <raw_dir>/<片名>.film.json，返回写入路径。

    文件格式与 H3 导演台既有约定一致：style / lang / lines / reuse。
    """
    film = safe_film_name(name)
    lines = split_lines(lines_text)
    if not lines:
        raise ConfigError("lines 至少需要一行台词")
    reuse = split_lines(reuse_text)

    root = Path(raw_dir) if raw_dir else resolve_raw_dir(load_config())
    if not root.is_dir():
        raise ConfigError(f"raw_dir 不存在或不可访问：{root}")

    target = root / f"{film}.film.json"
    if target.exists() and not overwrite:
        raise ConfigError(f"目标已存在：{target}（勾选 overwrite 才会覆盖）")

    payload = {
        "style": (style or "").strip(),
        "lang": (lang or "zh").strip() or "zh",
        "lines": lines,
        "reuse": reuse,
    }
    fd, tmp = tempfile.mkstemp(prefix=".film-", suffix=".json", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
    return target


def load_example() -> dict:
    try:
        return json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULTS)
