# -*- coding: utf-8 -*-
"""JEV 4B 自动决策：字幕字体 + 是否需要BGM + BGM情绪
用法: video_decide.py <state_json路径> <输出json路径>
state: {"style": "视频风格描述", "duration": 15, "has_dialogue": true}
"""
import json
import sys
import os

from neohorse_decision import DecisionEngine

MODEL_DIR = r"H:\JEV\NeoHorse-Jev-4B"

FONTS = {
    "雅黑": "现代通用，任何题材都稳，清晰度最高",
    "黑体": "硬朗厚重，动作/科技/热血题材",
    "楷体": "古风文艺，人像/抒情/东方美学",
    "宋体": "传统正式，历史/纪录/文化感",
    "仿宋": "清瘦复古，文艺清新/日系",
    "隶书": "书法韵味，武侠/国潮",
}


def pick(answer, criteria):
    """防御式解析 choice 答案：兼容 answer/value/label/概率最大项"""
    if not isinstance(answer, dict):
        return None
    for k in ("answer", "value", "label", "choice"):
        if answer.get(k):
            v = str(answer[k])
            if v in criteria:
                return v
    probs = answer.get("probabilities") or {}
    if probs:
        try:
            idx = max(probs, key=lambda k: float(probs[k]))
        except (ValueError, TypeError):
            return None  # 概率值非数值
        if idx in criteria:
            return idx  # choice 题：probabilities 键即选项名（引擎 _inference.py 的真实 schema）
        try:
            return list(criteria.keys())[int(idx)]  # score 题：probabilities 键为序号字符串
        except (ValueError, IndexError, TypeError):
            return None  # 序号越界 / 键名既非选项名也非序号
    return None


def main():
    if len(sys.argv) < 3:
        print("用法: video_decide.py <state_json路径> <输出json路径>", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], encoding="utf-8") as f:
        state = json.load(f)
    style = state.get("style", "通用短视频")
    dur = state.get("duration", 15)
    has_dlg = bool(state.get("has_dialogue", True))

    req = {
        "state": (f"一个{dur}秒的AI生成视频。风格：{style}。"
                  f"{'视频中有人物对白，' if has_dlg else '视频无对白，'}"
                  "需要为它选择字幕字体并决定是否配背景音乐和帧率补偿。"),
        "questions": {
            "font": {
                "type": "choice",
                "instructions": ("为该视频选择最匹配的字幕字体（用于中日双语字幕，"
                                 "需在任意画面上清晰可读、与视频风格协调）。"
                                 "字体须在画面上清晰可读——字号不得过小；"
                                 "字幕渲染层会按视频实际宽高自动折行防超边界，"
                                 "但所选字体本身要适配横屏/竖屏"
                                 "（竖屏人像特写慎用过窄的字体）。"),
                "criteria": dict(FONTS),
            },
            "bilingual": {
                "type": "choice",
                "instructions": (
                    "决定字幕是否双语。视频有外语对白且底部中文是翻译时选 yes；"
                    "对白纯中文或无对白时选 no（只保留底部中文，顶部留空）。"
                ),
                "criteria": {"yes": "视频含外语对白（如日语配音），顶部保留原文、底部中文翻译",
                             "no": "单一语言字幕（仅底部中文）"},
            },
            "interpolate": {
                "type": "choice",
                "instructions": (
                    "决定该视频是否需要帧率补偿（24fps 补到 48fps，运动更丝滑）。判断规则："
                    "规则1：有明显运动/运镜/动作/打斗/飞驰 → yes（补帧收益明显）；"
                    "规则2：静态特写/对话/表情表演为主 → no（补帧无收益，浪费处理时间）；"
                    "规则3：拿不准 → no（保守选择，不补不会差，补错浪费资源）。"
                ),
                "criteria": {"yes": "需要补帧，画面有明显运动", "no": "不需要，画面以静态为主"},
            },
            "bgm": {
                "type": "choice",
                "instructions": (
                    "决定该视频是否需要背景音乐。严格按以下规则判断："
                    "规则1：纯风景/氛围/展示类画面 → 默认 need；"
                    "规则2：有人物对白且对白是核心内容 → 默认 none（对白为主，BGM 干扰人声），"
                    "除非画面情绪强烈需要音乐铺垫（离别/高潮/开场等）；"
                    "规则3：拿不准 → 选 need（大众短视频审美中 BGM 常驻是主流，"
                    "系统会自动把音量压低并闪避人声，不会干扰对白）。"
                ),
                "criteria": {"need": "需要，低音量垫底且自动闪避人声", "none": "不需要，保持干净"},
            },
            "bgm_type": {
                "type": "choice",
                "instructions": (
                    "决定 BGM 形态。人物对白是核心内容的视频必须选 instrumental"
                    "（人声演唱干扰对白）；纯风景/氛围/展示类可按风格选 vocal 增强情绪。"
                ),
                "criteria": {"instrumental": "轻音乐/纯音乐（无人声，氛围铺底，不抢对白）",
                             "vocal": "有声音乐（含人声演唱，情绪传达更强）"},
            },
            "mood": {
                "type": "choice",
                "instructions": "若需要配乐，选择最匹配的音乐情绪。",
                "criteria": {"upbeat": "轻快电子/流行", "epic": "史诗管弦",
                             "gentle": "温柔钢琴/氛围", "tense": "紧张暗黑"},
            },
        },
    }
    degraded = False
    try:
        eng = DecisionEngine(MODEL_DIR, device="cpu")
        rec = eng.predict(req)
    except Exception as e:  # 引擎失败也要产出决策文件：降级默认值，别让流水线静默兜底
        print(f"⚠ JEV 引擎失败，降级默认决策（雅黑/bilingual=no/need/instrumental/gentle）: {e}", file=sys.stderr)
        rec, degraded = {}, True
    a = (rec or {}).get("answers") or {}

    font = pick(a.get("font"), FONTS) or "雅黑"
    interp_crit = {"yes": "需要补帧", "no": "不需要"}
    bgm_crit = {"need": "需要，BGM 增强氛围", "none": "不需要，保持干净"}
    mood_crit = {"upbeat": "轻快电子/流行", "epic": "史诗管弦",
                 "gentle": "温柔钢琴/氛围", "tense": "紧张暗黑"}
    bilingual_crit = {"yes": "含外语对白，双语字幕", "no": "仅底部中文"}
    bgm_type_crit = {"instrumental": "纯音乐/无人声", "vocal": "人声演唱"}
    interpolate = pick(a.get("interpolate"), interp_crit) or "no"
    bgm = pick(a.get("bgm"), bgm_crit) or "need"
    mood = pick(a.get("mood"), mood_crit) or "gentle"
    bilingual = pick(a.get("bilingual"), bilingual_crit) or "no"  # 解析失败默认单语
    bgm_type = pick(a.get("bgm_type"), bgm_type_crit) or "instrumental"  # 解析失败默认纯音乐

    # 决策依据透明化：各问题的完整概率分布（供完成弹窗/面板展示"决策思路"）
    reasons = {}
    for qname, crit in (("font", FONTS), ("bilingual", bilingual_crit),
                        ("interpolate", interp_crit), ("bgm", bgm_crit),
                        ("bgm_type", bgm_type_crit), ("mood", mood_crit)):
        ans = a.get(qname, {}) or {}
        probs = ans.get("probabilities") or {}
        reasons[qname] = {
            "chosen": crit.get(str(ans.get("answer") or ans.get("value")
                                  or ans.get("choice") or ""), None)
                      or {"need": "需要", "none": "不需要"}.get(str(ans.get("choice") or ""), None)
                      or "未解析",
            "confidence": ans.get("confidence"),
            "probabilities": probs,
        }

    out = {"font": font, "interpolate": interpolate, "bgm": bgm, "mood": mood,
           "bilingual": bilingual, "bgm_type": bgm_type, "style": style,
           "degraded": degraded, "reasons": reasons}
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
