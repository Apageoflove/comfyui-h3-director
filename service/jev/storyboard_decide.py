# -*- coding: utf-8 -*-
"""JEV 4B 长片分镜决策：是否分镜 / 段间转场 / 风格锚定 / 字体 / 整片配乐
用法: storyboard_decide.py <film_json路径> <输出json路径>
film json: {"style": str, "lang": "zh", "lines": [str | {top,bottom} ...]}
台词切段按每段 ≤3 行确定性完成（不劳 JEV）；JEV 只回答选择题：
①该请求是否需要分镜（时长>15s 或多场景 → 是，否则单片）
②每段边界转场 cut 还是 crossfade（场景连续 → cut；场景跳变 → crossfade）
③风格锚定模板（+字体/整片配乐，与 video_decide 同款选项）
"""
import json
import sys

from neohorse_decision import DecisionEngine

MODEL_DIR = r"H:\JEV\NeoHorse-Jev-4B"
MAX_LINES_PER_SEG = 3
EST_SEC_PER_LINE = 5.0

FONTS = {
    "雅黑": "现代通用，任何题材都稳，清晰度最高",
    "黑体": "硬朗厚重，动作/科技/热血题材",
    "楷体": "古风文艺，人像/抒情/东方美学",
    "宋体": "传统正式，历史/纪录/文化感",
    "仿宋": "清瘦复古，文艺清新/日系",
    "隶书": "书法韵味，武侠/国潮",
}

# 风格锚定：JEV 选模板，确定性拼进 style_anchor（4B 决策引擎不产自由文本）
ANCHORS = {
    "anime_cine": "电影级动漫：统一角色设定、稳定光影色调、景深镜头语言、细腻材质",
    "real_film": "写实电影：自然光影、胶片色调、浅景深、真实物理质感",
    "soft_illus": "柔和插画：低饱和暖色、柔光晕与空气感、圆润统一造型",
    "graphic_noir": "高对比图形化：明快色块、轮廓光、戏剧化布光与剪影设计",
}
ANCHOR_TEXT = {
    "anime_cine": "电影级动漫质感，统一的角色设定与作画细节，稳定的光影与色调，"
                  "细腻的景深与材质表现，全片画风连贯无漂移",
    "real_film": "写实电影质感，自然光影与胶片色调，稳定的人物形象与场景物理，"
                 "浅景深电影镜头语言，全片观感统一",
    "soft_illus": "柔和插画动漫质感，低饱和暖色调，柔和光晕与空气感，"
                  "人物造型圆润统一，全片画风连贯",
    "graphic_noir": "高对比图形化质感，明快的色块与轮廓光，稳定的人物剪影设计，"
                    "戏剧化布光，全片风格统一",
}


def pick(answer, criteria):
    """防御式解析 choice 答案：兼容 answer/value/label/概率最大项（与 video_decide 同款）"""
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
            return None
        if idx in criteria:
            return idx  # choice 题：probabilities 键即选项名
        try:
            return list(criteria.keys())[int(idx)]  # score 题：键为序号
        except (ValueError, IndexError, TypeError):
            return None
    return None


def norm_lines(raw):
    """台词行归一为 {top,bottom}；接受纯字符串或字典"""
    out = []
    for l in (raw or []):
        if isinstance(l, dict):
            top, bottom = str(l.get("top") or ""), str(l.get("bottom") or "")
        else:
            top, bottom = "", str(l)
        out.append({"top": top, "bottom": bottom})
    return out


def line_text(l):
    return (l.get("bottom") or l.get("top") or "").strip()


def build_scene_prompt(idx, total, anchor, texts):
    """确定性组段提示词：锚定风格 + 首帧衔接约束 + 台词推进 + 尾部动势（防减速尾卡顿）"""
    quoted = " / ".join(t for t in texts if t) or "（本段无台词，按画面情绪推进）"
    if idx == 0:
        head = f"这是长片的开场段（第1/{total}段），直接确立场景与基调"
    else:
        head = (f"这是长片的第{idx + 1}/{total}段，画面必须与上一段结尾无缝衔接："
                "延续同一场景、人物、机位与光线，从上一段的运动趋势自然续接")
    tail = ("段落结尾保持主体仍有运动趋势，不要减速趋向静止"
            if idx < total - 1 else "段落结尾情绪自然收束")
    return f"{head}。{anchor}。本段叙事依据台词推进：{quoted}。{tail}。"


def main():
    if len(sys.argv) < 3:
        print("用法: storyboard_decide.py <film_json路径> <输出json路径>", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], encoding="utf-8") as f:
        film = json.load(f)
    style = str(film.get("style") or "通用长片")
    lines = norm_lines(film.get("lines"))
    n = len(lines)
    est = int(n * EST_SEC_PER_LINE)
    chunks = ([lines[i:i + MAX_LINES_PER_SEG]
               for i in range(0, n, MAX_LINES_PER_SEG)] or [[]])
    m = len(chunks)

    seg_desc = " ｜ ".join(
        f"[段{i + 1}]" + "；".join(line_text(l) for l in c if line_text(l))
        for i, c in enumerate(chunks))
    state = (f"一部AI生成长片订单。风格：{style}。共 {n} 行台词，按语速预估整片约 {est} 秒；"
             f"生成端每段固定 15 秒，按每段最多 {MAX_LINES_PER_SEG} 行台词确定性切为 {m} 段。"
             f"各段台词：{seg_desc}")

    questions = {
        "storyboard": {
            "type": "choice",
            "instructions": (
                "判断该订单是否需要分镜（切多段生成后拼接）。规则："
                "规则1：预估时长 >15 秒 → yes；"
                "规则2：台词明显跨多个场景/情节节点 → yes；"
                "规则3：预估 ≤15 秒且单一场景一次性叙事 → no（单片直出）。"
            ),
            "criteria": {"yes": "需要分镜：切多段串行生成再拼接",
                         "no": "单片即可：单次 15 秒生成覆盖全部内容"},
        },
        "anchor": {
            "type": "choice",
            "instructions": "为全片选定统一的风格锚定方向（用于拼进每段提示词，保证段间画风一致）。",
            "criteria": dict(ANCHORS),
        },
        "font": {
            "type": "choice",
            "instructions": "为长片字幕选择字体（用于多段字幕烧录，须在任意画面上清晰可读、与风格协调）。",
            "criteria": dict(FONTS),
        },
        "bgm": {
            "type": "choice",
            "instructions": (
                "决定整片是否需要铺一条完整背景音乐（成片后一次混入，自动压低并闪避人声）。"
                "规则：对白是核心内容且情绪平缓 → none；长片有整体情绪线/开场结尾需要音乐铺垫 → need；"
                "拿不准 → need（系统自动闪避人声，不会干扰对白）。"
            ),
            "criteria": {"need": "需要，整片铺底且自动闪避人声", "none": "不需要，保持分段原生音频"},
        },
        "bgm_type": {
            "type": "choice",
            "instructions": "若需要配乐，决定 BGM 形态：对白为核心必须选 instrumental（人声演唱干扰对白）。",
            "criteria": {"instrumental": "轻音乐/纯音乐（无人声铺底）",
                         "vocal": "有声音乐（含人声演唱）"},
        },
        "mood": {
            "type": "choice",
            "instructions": "若需要配乐，选择整片音乐情绪。",
            "criteria": {"upbeat": "轻快电子/流行", "epic": "史诗管弦",
                         "gentle": "温柔钢琴/氛围", "tense": "紧张暗黑"},
        },
    }
    # 段间转场：每条边界一题（场景连续→cut；场景跳变→crossfade）
    for i in range(1, m):
        a, b = line_text(chunks[i - 1][-1]) if chunks[i - 1] else "", \
               line_text(chunks[i][0]) if chunks[i] else ""
        questions[f"trans{i}"] = {
            "type": "choice",
            "instructions": (
                f"判断段{i}与段{i + 1}的边界转场。上段结尾台词「{a}」，下段开头台词「{b}」。"
                "规则：两段在同一场景连续叙事（同地点同人物，动作/对话顺延）→ cut"
                "（硬切零损失，首帧传递链已保证画面衔接）；"
                "两段之间场景跳变（换地点/时间流逝/场面切换）→ crossfade（0.5秒交叉淡化）。"
            ),
            "criteria": {"cut": "硬切：场景连续，画面天然衔接",
                         "crossfade": "交叉淡化 0.5s：场景跳变，掩盖画风/色调微差"},
        }

    req = {"state": state, "questions": questions}
    degraded = False
    answers = {}
    try:
        eng = DecisionEngine(MODEL_DIR, device="cpu")
        # 1) 全局问题（是否分镜/风格锚/字体/BGM/形态/情绪）
        base_keys = ("storyboard", "anchor", "font", "bgm", "bgm_type", "mood")
        rec = eng.predict({"state": state,
                           "questions": {k: questions[k] for k in base_keys}})
        answers.update((rec or {}).get("answers") or {})
    except Exception as e:  # 引擎失败也产出决策文件：降级默认值（全 cut + 确定性分镜）
        print(f"⚠ JEV 引擎失败，降级默认分镜决策（切分/硬切/雅黑/need/instrumental/gentle）: {e}",
              file=sys.stderr)
        degraded = True
    # 2) 转场问题分批（长片边界多，单次请求会触发 "Too many questions"）
    trans_q = {k: v for k, v in questions.items() if k.startswith("trans")}
    if not degraded and trans_q:
        keys = list(trans_q)
        for j in range(0, len(keys), 5):
            batch = {k: trans_q[k] for k in keys[j:j + 5]}
            try:
                r2 = eng.predict({
                    "state": f"长片分镜：共 {m} 段，判断相邻段的边界转场方式。",
                    "questions": batch})
                answers.update((r2 or {}).get("answers") or {})
            except Exception as e:
                print(f"⚠ 转场决策分批失败（这批默认 cut）: {e}", file=sys.stderr)
    a = answers

    need = pick(a.get("storyboard"), {"yes": 1, "no": 1}) or \
        ("yes" if (est > 15 or m > 1) else "no")
    anchor_key = pick(a.get("anchor"), ANCHORS) or "anime_cine"
    font = pick(a.get("font"), FONTS) or "雅黑"
    bgm = pick(a.get("bgm"), {"need": 1, "none": 1}) or "need"
    bgm_type = pick(a.get("bgm_type"), {"instrumental": 1, "vocal": 1}) or "instrumental"
    mood = pick(a.get("mood"), {"upbeat": 1, "epic": 1, "gentle": 1, "tense": 1}) or "gentle"
    if need != "yes":
        chunks = [lines] or [[]]  # 单片：全部台词一段
    total = len(chunks)
    anchor = f"{style}。{ANCHOR_TEXT[anchor_key]}"

    segments = []
    for i, chunk in enumerate(chunks):
        trans = "cut"
        if need == "yes" and 0 < i + 1 < total:
            trans = pick(a.get(f"trans{i + 1}"),
                         {"cut": 1, "crossfade": 1}) or "cut"
        segments.append({
            "idx": i,
            "scene_prompt": build_scene_prompt(i, total, anchor,
                                               [line_text(l) for l in chunk]),
            "lines": chunk,
            "transition_to_next": trans,  # 末段该字段无消费者，恒 cut
        })

    reasons = {}
    for qname, crit in (("storyboard", {"yes": "需要", "no": "单片"}),
                        ("anchor", ANCHORS), ("font", FONTS),
                        ("bgm", {"need": "需要", "none": "不需要"}),
                        ("bgm_type", {"instrumental": "纯音乐", "vocal": "人声演唱"}),
                        ("mood", {"upbeat": "轻快电子", "epic": "史诗管弦",
                                  "gentle": "温柔钢琴", "tense": "紧张暗黑"})):
        ans = a.get(qname, {}) or {}
        reasons[qname] = {"answer": ans.get("answer") or ans.get("value")
                          or ans.get("choice"),
                          "confidence": ans.get("confidence"),
                          "probabilities": ans.get("probabilities") or {}}
    for i in range(1, m):
        ans = a.get(f"trans{i}", {}) or {}
        reasons[f"trans{i}"] = {"answer": ans.get("answer") or ans.get("choice"),
                                "probabilities": ans.get("probabilities") or {}}

    out = {"need_storyboard": need == "yes", "style_anchor": anchor,
           "font": font, "bgm": bgm, "bgm_type": bgm_type, "mood": mood,
           "segments": segments, "n_lines": n, "est_duration_s": est,
           "degraded": degraded, "reasons": reasons}
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({"need_storyboard": out["need_storyboard"],
                      "segments": len(segments), "anchor": anchor_key,
                      "bgm": bgm, "font": font, "degraded": degraded},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
