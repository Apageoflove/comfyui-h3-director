# -*- coding: utf-8 -*-
"""H3 官方提示词 skill 的最小本地适配层。

来源：MiniMax-AI/MiniMax-H3 的官方可移植 skill `h3-prompt-writing`
（SKILL.md + references/base-en.txt + references/ref-en.txt）。
本模块只做“结构封装/校验”，不调用任何外部 API，也不替换 H3 节点。

默认行为不变：只有满足以下任一条件才会改写提示词
  1) 环境变量 H3_PROMPT_SKILL=1/true/yes/on
  2) guard profile 里 prompt_skill=true
否则 maybe_enhance() 原样返回输入。

与 H3ConsistencyGuard 的关系：先做本模块的结构封装，再交给
h3_guard.build_prompt() 注入角色/服装/音色硬约束；守卫逻辑不变。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

SKILL_NAME = "h3-prompt-writing"
SKILL_DIRS = [
    Path(r"H:\zcodecache\.zcode\skills\h3-prompt-writing"),
    Path(r"H:\H3DirectorDesk\tools\h3_prompt_writing"),
    Path(r"D:\H3DirectorDesk\tools\h3_prompt_writing"),
]

BASE_FIELDS = (
    "integrated_multimodal_description",
    "overall_soundscape",
    "non_diegetic_music",
)
REF_FIELDS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)

_TRUTHY = {"1", "true", "yes", "on"}


def skill_dir() -> Path | None:
    for d in SKILL_DIRS:
        if (d / "SKILL.md").is_file() and (d / "references" / "base-en.txt").is_file():
            return d
    return None


def skill_available() -> bool:
    return skill_dir() is not None


def skill_hashes() -> dict:
    d = skill_dir()
    if not d:
        return {}
    out = {}
    for rel in ("SKILL.md", "references/base-en.txt", "references/ref-en.txt", "agents/openai.yaml"):
        p = d / rel
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _structured(text: str, fields=BASE_FIELDS) -> bool:
    low = (text or "").lower()
    return all(f"{name.lower()}:" in low for name in fields)


def _field_order_ok(text: str, fields=BASE_FIELDS) -> bool:
    low = (text or "").lower()
    pos = [low.find(f"{name.lower()}:") for name in fields]
    return all(p >= 0 for p in pos) and pos == sorted(pos)


def detect_mode(profile: dict | None = None, explicit: str | None = None) -> str:
    if explicit:
        return explicit.upper()
    prof = profile or {}
    seg = prof.get("_segment") or {}
    if prof.get("last_frame") or seg.get("last_frame"):
        return "FL2VA"
    if prof.get("references") or seg.get("references") or prof.get("ref_images"):
        return "REF2VA"
    if prof.get("start_frame") or seg.get("start_frame"):
        return "I2VA"
    return "T2VA"


def _instruction(mode: str, duration: float, shot_n: int = 1) -> str:
    sec = max(0.0, float(duration or 5.0))
    if mode == "I2VA":
        return ("For the target video, at 0.00 seconds into the target video, "
                "<Picture 1> (from [Shot 1]) is fully referenced.")
    if mode == "FL2VA":
        return ("How the reference pictures align with the target video — "
                f"Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
                f"Picture 2 (from Shot {shot_n}) aligns with the {sec:.2f}-second mark of the target video.")
    if mode == "L2VA":
        return ("How the reference pictures align with the target video — "
                f"<Picture 1> (from [Shot {shot_n}]) aligns with the {sec:.2f}-second mark of the target video.")
    return ""


def _clean_base(text: str) -> str:
    text = (text or "").strip()
    # 已有官方字段时不再二次封装
    if _structured(text):
        return text
    # 去掉可能残留的旧字段名，避免重复
    text = re.sub(r"(?im)^\s*(integrated_multimodal_description|overall_soundscape|non_diegetic_music)\s*:\s*", "", text)
    return text.strip()


def enhance(
    text: str,
    *,
    mode: str = "T2VA",
    duration: float = 5.0,
    style: str = "",
    soundscape: str = "",
    music: str = "N/A",
    shot_n: int = 1,
    labels: dict | None = None,
) -> dict:
    """把自由文本封装成官方 H3 prompt 结构；已结构化则原样通过。

    返回 {"prompt", "mode", "structured_before", "warnings"}。
    不翻译用户文本；对白/画面文字保持原语言，符合官方“对白保留原语言”的要求。
    """
    mode = (mode or "T2VA").upper()
    raw = (text or "").strip()
    warnings = []
    if not raw:
        return {"prompt": "", "mode": mode, "structured_before": False, "warnings": ["empty prompt"]}

    before = _structured(raw, REF_FIELDS if mode == "REF2VA" else BASE_FIELDS)
    if before:
        fields = REF_FIELDS if mode == "REF2VA" else BASE_FIELDS
        if not _field_order_ok(raw, fields):
            warnings.append("field order differs from official guide")
        return {"prompt": raw, "mode": mode, "structured_before": True, "warnings": warnings}

    base = _clean_base(raw)
    if duration and not (4.0 <= float(duration) <= 15.0):
        warnings.append(f"duration {duration}s outside official 4-15s guidance")
    if len(base) > 1800:
        warnings.append("base description is long; consider splitting shots")

    if mode == "REF2VA":
        labels = labels or {}
        p1 = labels.get("picture1", "<Picture 1>")
        subject = labels.get("subject", "the main subject")
        prompt = "\n".join([
            f"subject_definitions: {p1} defines {subject}; its identity, clothing, and key visual attributes must remain consistent across all shots.",
            f"summary: {base}",
            f"retention_analysis: {p1} is retained in every shot where the subject appears; no unlabeled reference is used.",
            f"detailed_description: [Shot 1] {base}",
            f"overall_soundscape: {soundscape or 'Natural ambience and physical action sounds consistent with the scene.'}",
            f"non_diegetic_music: {music or 'N/A'}",
        ])
        return {"prompt": prompt, "mode": mode, "structured_before": False, "warnings": warnings}

    head = _instruction(mode, duration, shot_n)
    has_shot = bool(re.match(r"^\[Shot\s*1\]", base, re.I))
    style_txt = style.strip().rstrip("。.;；")
    if has_shot:
        body = base
        if style_txt:
            # 风格放在 Shot 1 开头，官方建议先声明整体风格与初始构图
            body = re.sub(r"^\[Shot\s*1\]\s*", f"[Shot 1] {style_txt}. ", body,
                          count=1, flags=re.I)
    else:
        body = f"[Shot 1] {style_txt}. {base}" if style_txt else f"[Shot 1] {base}"
    prompt = "\n".join([
        head,
        "",
        f"integrated_multimodal_description: {body}",
        "",
        f"overall_soundscape: {soundscape or 'Natural ambience and physical action sounds consistent with the scene.'}",
        "",
        f"non_diegetic_music: {music or 'N/A'}",
    ]).strip()
    if head == "":
        prompt = "\n".join([
            f"integrated_multimodal_description: {body}",
            "",
            f"overall_soundscape: {soundscape or 'Natural ambience and physical action sounds consistent with the scene.'}",
            "",
            f"non_diegetic_music: {music or 'N/A'}",
        ])
    return {"prompt": prompt, "mode": mode, "structured_before": False, "warnings": warnings}


def validate(prompt: str, mode: str = "T2VA") -> dict:
    mode = (mode or "T2VA").upper()
    fields = REF_FIELDS if mode == "REF2VA" else BASE_FIELDS
    problems = []
    if not _field_order_ok(prompt, fields):
        problems.append("missing or out-of-order fields: " + ", ".join(fields))
    if re.search(r"<Picture\s+\d+>", prompt or "") and "<Picture 1>" not in (prompt or ""):
        problems.append("reference labels may be inconsistent")
    return {"ok": not problems, "problems": problems, "mode": mode, "fields": list(fields)}


def _enabled(profile: dict | None = None) -> bool:
    if os.environ.get("H3_PROMPT_SKILL", "").strip().lower() in _TRUTHY:
        return True
    prof = profile or {}
    return bool(prof.get("prompt_skill"))


def maybe_enhance(base_prompt: str, profile: dict | None = None, seg_idx: int = 1) -> str:
    """默认原样返回；仅在 H3_PROMPT_SKILL 或 profile.prompt_skill 开启时封装。"""
    if not _enabled(profile):
        return base_prompt
    prof = dict(profile or {})
    seg = ((prof.get("segments") or {}).get(str(int(seg_idx))) or {})
    # 段级配置优先
    merged = dict(prof)
    merged["_segment"] = seg
    mode = detect_mode(merged)
    duration = float(seg.get("duration") or prof.get("duration") or 5.0)
    style = str(seg.get("style") or prof.get("style") or "")
    out = enhance(base_prompt, mode=mode, duration=duration, style=style, shot_n=max(1, int(seg_idx)))
    return out.get("prompt") or base_prompt


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="H3 official prompt skill local adapter")
    p.add_argument("text", nargs="?", help="base prompt text; omit to read stdin")
    p.add_argument("--mode", default="T2VA", choices=["T2VA", "I2VA", "FL2VA", "L2VA", "REF2VA", "Ref2VA"])
    p.add_argument("--duration", type=float, default=5.0)
    p.add_argument("--style", default="")
    p.add_argument("--check", action="store_true", help="validate output structure")
    a = p.parse_args(argv)
    text = a.text if a.text is not None else __import__("sys").stdin.read()
    res = enhance(text, mode=a.mode.upper(), duration=a.duration, style=a.style)
    print(res["prompt"])
    if a.check:
        v = validate(res["prompt"], a.mode.upper())
        print(json.dumps(v, ensure_ascii=False))
        return 0 if v["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
