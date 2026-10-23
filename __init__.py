# -*- coding: utf-8 -*-
"""ComfyUI-H3Director —— 把 H3 导演台作为 ComfyUI 插件接入的最小实现。

零侵入约定：
- 只使用 ComfyUI 公开扩展点：NODE_CLASS_MAPPINGS / WEB_DIRECTORY / PromptServer.routes；
- 不 monkey-patch 队列、采样器或任何现有节点；
- 不写入 ComfyUI 安装目录；配置与缓存都在插件目录或用户指定的 raw_dir；
- 卸载 = 删除本文件夹（外加删掉 config.json）。
"""
from __future__ import annotations

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .version import __version__

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY", "__version__"]

# 只读配置接口，供侧栏面板取默认 URL；注册失败绝不能影响 ComfyUI 启动。
try:
    from aiohttp import web
    from server import PromptServer

    from .config import load_config

    @PromptServer.instance.routes.get("/h3director/config")
    async def _h3director_config(request):  # noqa: ARG001
        cfg = load_config()
        return web.json_response({
            "url": cfg["director_url"],
            "raw_dir": cfg["raw_dir"],
            "timeout": cfg["timeout"],
            "version": __version__,
        })
except Exception as exc:  # pragma: no cover - ComfyUI 未就绪时静默降级
    print(f"[H3Director] config route disabled: {exc}")
