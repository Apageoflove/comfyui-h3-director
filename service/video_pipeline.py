# -*- coding: utf-8 -*-
r"""H3 导演台（Director's Desk）
ComfyUI 出片(H:\h3cache\raw) → 语义决策 → 帧率重构 → 质量评估 → 音色转换 → 字幕合成 → 配乐混音 → 交付归档(H:\h3cache)
- 台词/风格配置: D:\H3DirectorDesk\dialogue.json（视频同名 .dialogue.json 可按片覆盖）
- 多任务: raw 目录多个视频进入 FIFO 队列，单工作线程串行执行（防显存/内存打满），
  每条任务独立状态机互不混淆，控制台按线路展示
- 音色: 每片手动把控「音色·关/开」（POST /task/voice，默认 off）；Seed-VC 独立
  venv/CLI，只替换音轨（-c:v copy），硬闸（时长/Whisper 文本/F0/性别/峰值/视频流）
  不过自动回退原音轨；单条在字幕对齐前完成，字幕按最终音色重新对齐；长片只在
  concat 后、BGM 前处理最终片，按 manifest 段边界 + profile speakers/voice_ref
  逐角色转换，无可靠 speaker 范围则跳过并记录，绝不整片同声。
- 配乐: JEV 判定 → 曲库检索 → 无现成音乐自动唤起 ACE-Step 引擎出曲 → 人声闪避混音
  （每片可手动把控：线路上「配乐·自动/关/开」三态按钮，POST /task/bgm，优先级高于 JEV；
  关=本片跳过，开=本片强制配，自动=听 JEV。无全局开关。BGM 永远在音色之后混入。）
- 控制台: http://127.0.0.1:8898 （导演台节点轨 + 每步进度/ETA + 流式日志 + 下载成片）
"""
import os
import re
import sys
import time
import json
import glob
import difflib
import wave
import random
import shutil
import threading
import subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

RAW = r"H:\h3cache\raw"
OUT = r"H:\h3cache"
ORIG = os.path.join(OUT, "原始")
WORK = os.path.join(OUT, "work")
FAIL = os.path.join(OUT, "失败")
DL = r"D:\H3DirectorDesk"                 # 程序/配置（D 盘）
CACHE = r"H:\h3cache\h3_cache"            # 运行状态/日志/历史（H 盘缓存区）
DOWNLOADS = r"H:\h3cache\downloads"   # 浏览器下载落点：服务端镜像 + Edge app 默认下载目录
DIALOGUE = os.path.join(DL, "dialogue.json")
STATUS = os.path.join(CACHE, "pipeline_status.json")
ETA_PATH = os.path.join(CACHE, "pipeline_eta.json")
CONFIG_PATH = os.path.join(CACHE, "pipeline_config.json")  # 已废弃，保留路径常量防外部引用
HISTORY_PATH = os.path.join(CACHE, "pipeline_history.json")
LOGF = os.path.join(CACHE, "pipeline.log")
FFMPEG = r"D:\ComfyUI\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
JUDGE_SCRIPT = r"H:\JEV\h3_judge.py"
JEV_PY = r"H:\JEV\venv\Scripts\python.exe"
DECIDE_SCRIPT = r"H:\JEV\video_decide.py"
GEN_SONG = r"H:\MusicAI\scripts\gen_song.py"
ACE_PY = r"H:\MusicAI\envs\acestep\Scripts\python.exe"
BGM_OUT = r"H:\h3cache\bgm"
BGM_DIRS = [BGM_OUT, r"H:\MusicAI\outputs"]
COMFY = "http://127.0.0.1:8188"
PORT = 8898
POLL = 4
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW：pythonw 下子进程不闪控制台

# ── 长片（film）编排：分镜决策 → 逐段生成→字幕→质检 → 拼接 → 整片配乐 → 交付 ──
FILMS = os.path.join(OUT, "films")                # H:\h3cache\films\<片名>\{seg_XX.mp4, <片名>.mp4, manifest.json}
STORYBOARD_SCRIPT = r"H:\JEV\storyboard_decide.py"
FILM_TEMPLATE = os.path.join(DL, "h3_film_seg_template.json")  # API 格式 H3 i2v（_probe\h3_i2v_api.json 验证版）
FILM_SEG_SEC = 15                                 # 每段时长（秒，经 107 ComfyMath 换算 17 帧网格）
FILM_SEG_PREFIX = "filmseg_"                      # SaveVideo 前缀：detector 据此跳过段产物，不走单片流水线
FILM_MAX_RETRIES = 2                              # 质检不过关换 seed 原位重跑上限（重跑只换内容，拼接顺序永不改变）
FILM_GEN_TIMEOUT = 2700                           # 单段 ComfyUI 生成超时（秒）
FILM_SEG_ETA_DEFAULT = 480                        # 单段预估兜底（秒）；有实际/历史时被覆盖
FILM_TAIL_ETA = {"concat": 90, "voice": 120, "music": 300, "deliver": 15}  # 收尾阶段权重（秒）
COMFY_INPUT = r"D:\ComfyUI\input"                 # LoadImage 取图目录（首帧直接落此处，免 multipart 上传）
FILM_PHASE_LABEL = {"decide": "分镜决策", "generate": "逐段生成", "concat": "拼接合成",
                    "voice": "音色转换", "music": "整片配乐", "deliver": "交付归档"}
GUARD_DIR = os.path.join(DL, "tools", "h3_guard_profiles")
VOICE_PROFILE_DIRS = [r"H:\H3DirectorDesk\tools\h3_voice_profiles",
                      r"D:\H3DirectorDesk\tools\h3_voice_profiles"]
VOICE_TOOLS = os.path.join(DL, "tools")


def film_guard_profile(name):
    """按片名查找 <profile>.guard.json 里的 film 字段；没有则返回 None。"""
    try:
        for p in glob.glob(os.path.join(GUARD_DIR, "*.guard.json")):
            try:
                with open(p, encoding="utf-8") as fh:
                    d = json.load(fh)
                if str(d.get("film") or "") == name:
                    return p
            except Exception:
                continue
    except Exception:
        pass
    return None


def film_guard_prompt(prompt, profile, idx, repair=False):
    """仅对长片“生成模式”做生成前提示词注入；失败原样返回，绝不阻断生成。"""
    if not profile:
        return prompt
    try:
        if os.path.join(DL, "tools") not in sys.path:
            sys.path.insert(0, os.path.join(DL, "tools"))
        import h3_guard
        prof = h3_guard.load_profile(profile)
        base = prompt
        try:
            # 可选：官方 h3-prompt-writing 结构封装；默认关闭，守卫逻辑不变
            import h3_prompt_skill
            base = h3_prompt_skill.maybe_enhance(prompt, prof, int(idx) + 1)
        except Exception as e:
            log(f"[prompt-skill] 提示词封装失败，使用原提示词: {e}")
        res = h3_guard.build_prompt(base, prof, int(idx) + 1, repair=bool(repair))
        return res.get("prompt") or prompt
    except Exception as e:
        log(f"[guard] 长片提示词注入失败，使用原提示词: {e}")
        return prompt


def _voice_mod():
    """懒加载音色层（tools/h3_voice.py）；任何异常由调用方按「跳过音色」处理"""
    if VOICE_TOOLS not in sys.path:
        sys.path.insert(0, VOICE_TOOLS)
    import h3_voice
    return h3_voice


FONTS = {
    "雅黑": {"file": r"C:\Windows\Fonts\msyhbd.ttc", "family": "Microsoft YaHei"},
    "黑体": {"file": r"C:\Windows\Fonts\simhei.ttf", "family": "SimHei"},
    "楷体": {"file": r"C:\Windows\Fonts\simkai.ttf", "family": "KaiTi"},
    "宋体": {"file": r"C:\Windows\Fonts\simsun.ttc", "family": "SimSun"},
    "仿宋": {"file": r"C:\Windows\Fonts\simfang.ttf", "family": "FangSong"},
    "隶书": {"file": r"C:\Windows\Fonts\SIMLI.TTF", "family": "LiSu"},
}
MOOD_SHORT = {"upbeat": "轻快电子", "epic": "史诗管弦", "gentle": "温柔钢琴", "tense": "紧张暗黑"}
MOOD_CAPTIONS = {
    "upbeat": "轻快电子流行，节奏明快，现代感",
    "epic": "史诗管弦，大气磅礴，电影感",
    "gentle": "温柔钢琴氛围，舒缓优美",
    "tense": "紧张暗黑氛围电子，压迫感",
}

# 制作标准阶段：(key, 名称, 副标题, 初始预估秒数——随实际运行自适应)
# 音色在 QC 判定之后、字幕对齐之前执行：字幕必须按最终音色重新对齐（硬闸要求）。
STAGES = [
    ("decide",   "语义决策", "JEV-4B · 策略推理",      45),
    ("interp",   "帧率重构", "minterpolate · 24→48fps", 40),
    ("qc",       "质量评估", "JEV-4B · 抽帧评分",      210),
    ("voice",    "音色转换", "Seed-VC · 角色音色",      240),
    ("subtitle", "字幕合成", "Whisper · 按最终音色对齐", 75),
    ("bgm",      "配乐混音", "ACE-Step · 闪避混音",    300),
    ("deliver",  "交付归档", "归档落盘",                10),
]
STAGE_KEYS = [s[0] for s in STAGES]
STAGE_INFO = {s[0]: {"label": s[1], "desc": s[2]} for s in STAGES}
STAGE_DEFAULT = {s[0]: s[3] for s in STAGES}

_lock = threading.Lock()
# 配乐为「每片手动把控」：状态挂在任务上（auto=听 JEV / off=本片强制关 / on=本片强制开），
# 经 POST /task/bgm 设置；无全局开关。
_queue = []                 # FIFO 待处理文件名（含 <片名>.film.json 长片订单，worker 按后缀分发）
_queue_cv = threading.Condition()
_tasks = {}                 # base -> 任务状态机
_films = {}                 # 片名 -> 长片任务状态机（与 _tasks 并存，互不干扰）
_film_registered = set()    # 已入列的 .film.json 文件名（防重复触发）
_events = []                # 全局事件流 [{seq,ts,task,stage,text}]
_seq = 0
_eta = dict(STAGE_DEFAULT)  # 自适应阶段耗时（秒）
_last_result = {}
_history_records = []             # 任务终态历史（上限 200 条，新的在后）
_history_lock = threading.Lock()  # 历史增删/落盘串行化（/status 读取仍走 _lock）
_overall = {"phase": "idle", "text": "待片中 · 盯住 raw 目录"}
_t0 = time.time()


def log(msg):
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    try:
        if os.path.exists(LOGF) and os.path.getsize(LOGF) > 400_000:
            os.replace(LOGF, LOGF + ".1")
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def ev(task, stage, text):
    """向全局事件流追加一条（面板/日志按 task 过滤即得该线路的流式输出）"""
    global _seq
    with _lock:
        _seq += 1
        _events.append({"seq": _seq, "ts": time.time(),
                        "task": task, "stage": stage, "text": text})
        del _events[:-300]


def _task_json(t):
    return {"name": t["name"], "state": t["state"], "pos": t["pos"],
            "queued_at": t["queued_at"], "started_at": t["started_at"],
            "ended_at": t["ended_at"], "cur": t["cur"], "pct": t["pct"],
            "verdict": t["verdict"], "output": t["output"], "error": t["error"],
            "bgm_manual": t.get("bgm_manual", "off"),
            "voice_manual": t.get("voice_manual", "off"),
            "voice": t.get("voice"),
            "stages": {k: dict(v) for k, v in t["stages"].items()}}


_FILM_PHASE_ORDER = ["decide", "generate", "concat", "voice", "music", "deliver"]


def _film_hist_seg_eta():
    """历史长片折算的单段耗时（秒）：duration_s / 段数，取近 60 条的中位数"""
    vals = []
    for r in _history_records[-60:]:
        if not r.get("film") or r.get("state") != "done":
            continue
        n = len(r.get("segments") or [])
        dur = r.get("duration_s")
        if n and isinstance(dur, (int, float)) and dur > 0:
            vals.append(dur / n)
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def _film_eta(f, now=None):
    """长片 ETA（秒）：已完成段实际耗时定单段预估，当前段按时钟折算，收尾阶段按权重。
    仅 running 且有段时返回；其它状态返回 None（已完成长片前端不显示 ETA）。
    稳定字段：seg_index（当前段，-1=段间）/ seg_eta_s / seg_remaining_s /
    film_remaining_s / source（actual|history|default）/ updated_at"""
    if f.get("state") != "running" or not f.get("segments"):
        return None
    now = now or time.time()
    segs = f["segments"]
    done = [s for s in segs if s.get("state") == "done"]
    actual = [s["elapsed_s"] for s in done
              if isinstance(s.get("elapsed_s"), (int, float)) and s["elapsed_s"] > 5]
    hist = _film_hist_seg_eta()
    if actual:
        per, source = sum(actual) / len(actual), "actual"
    elif hist:
        per, source = hist, "history"
    else:
        per, source = FILM_SEG_ETA_DEFAULT, "default"
    active = -1
    for i, s in enumerate(segs):
        if s.get("state") not in ("done", "pending", "fail"):
            active = i
            break
    seg_remaining = None
    if active >= 0:
        st = segs[active].get("seg_started_at")
        elapsed = max(now - st, 0) if isinstance(st, (int, float)) else 0
        seg_remaining = max(per - elapsed, 0)
        rest = len(segs) - len(done) - 1  # 除当前段外的待做段
    else:
        rest = len(segs) - len(done)
    try:
        pi = _FILM_PHASE_ORDER.index(f.get("phase"))
    except ValueError:
        pi = 0
    tail = sum(FILM_TAIL_ETA[k] for k in _FILM_PHASE_ORDER[pi + 1:]
               if k in FILM_TAIL_ETA)
    film_remaining = max((seg_remaining or 0) + max(rest, 0) * per + tail, 0)
    return {"seg_index": active,
            "seg_eta_s": round(per),
            "seg_remaining_s": (round(seg_remaining)
                                if seg_remaining is not None else None),
            "film_remaining_s": round(film_remaining),
            "source": source, "updated_at": round(now, 1)}


def _film_json(f):
    """长片任务快照（/status 的 films 数组元素）"""
    return {"name": f["name"], "state": f["state"], "pos": f["pos"],
            "queued_at": f["queued_at"], "started_at": f["started_at"],
            "ended_at": f["ended_at"], "phase": f["phase"],
            "phase_label": FILM_PHASE_LABEL.get(f["phase"], f["phase"] or ""),
            "phase_text": f["phase_text"], "pct": f["pct"],
            "error": f["error"], "output": f["output"],
            "need_storyboard": f.get("need_storyboard"),
            "style_anchor": f.get("style_anchor"),
            "note": f.get("note") or "",
            "voice": f.get("voice"),
            "eta": _film_eta(f),  # 已完成/排队/无段 → None，前端据此不显示
            "segments": [dict(s) for s in f["segments"]]}


def _flush():
    with _lock:
        order = {"running": 0, "queued": 1, "error": 2, "done": 3}
        tasks = sorted(_tasks.values(),
                       key=lambda t: (order.get(t["state"], 4),
                                      -(t["ended_at"] or t["queued_at"] or 0)))
        snap = {"now": time.time(), "overall": _overall,
                "stages": [{"key": k, "label": STAGE_INFO[k]["label"],
                            "desc": STAGE_INFO[k]["desc"],
                            "eta": max(_eta.get(k, 30), 5)} for k in STAGE_KEYS],
                "tasks": [_task_json(t) for t in tasks],
                "films": [_film_json(f) for f in _films.values()],  # 长片任务（异常不影响快照）
                "events": _events[-160:],
                "history": list(reversed(_history_records))[:100],
                "last_result": _last_result}
        tmp = STATUS + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, STATUS)
        except OSError:
            pass
    return snap


def _load_eta():
    global _eta, _last_result
    try:
        with open(ETA_PATH, encoding="utf-8") as f:
            d = json.load(f)
        for k, v in (d.get("eta") or {}).items():
            if k in STAGE_DEFAULT and isinstance(v, (int, float)) and 3 < v < 7200:
                _eta[k] = float(v)
        _last_result = d.get("last_result") or {}
    except Exception:
        pass


def _save_eta():
    try:
        with open(ETA_PATH, "w", encoding="utf-8") as f:
            json.dump({"eta": _eta, "last_result": _last_result}, f, ensure_ascii=False)
    except OSError:
        pass


def _load_history():
    global _history_records
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):
            _history_records = [r for r in d if isinstance(r, dict)][-200:]
    except Exception:
        pass


def _write_history_locked():
    """调用方须持有 _history_lock：tmp+os.replace 原子落盘；失败抛异常"""
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_history_records, f, ensure_ascii=False)
    os.replace(tmp, HISTORY_PATH)


def _append_history(rec):
    """上限 200 条 · tmp+os.replace 原子落盘；异常不影响主流程"""
    global _history_records
    try:
        with _history_lock:
            _history_records.append(rec)
            del _history_records[:-200]
            _write_history_locked()
    except Exception:
        pass


def _artifact_target(name, is_film):
    """历史记录名 → (目标路径, 允许根, 长片 base)；名字非法返回 None。
    只接受纯名称：拒绝路径分隔符、盘符、上跳、首尾空白与 NUL。
    单片与长片按记录标志区分（记录名以 .film 结尾时剥离后缀）。"""
    if not isinstance(name, str) or not name or name != name.strip() or "\x00" in name:
        return None
    if "/" in name or "\\" in name or ":" in name or name in (".", ".."):
        return None
    if is_film:
        base = name[:-5] if name.endswith(".film") else name
        if not base or base in (".", ".."):
            return None
        return os.path.join(FILMS, base), FILMS, base
    return os.path.join(OUT, name), OUT, None


def _within_root(path, root):
    """path 严格位于 root 之下（大小写不敏感；盘符不同直接拒绝）"""
    p, r = os.path.normcase(path), os.path.normcase(root)
    if p == r:
        return False
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:
        return False


def _record_history(base, state, t0, verdict, output, error,
                    dec, interp_on, bilingual, bgm_type, bgm_track, bgm_manual,
                    voice_manual="off", voice=None):
    """任务终态（done/error）构造历史记录；任何异常吞掉，绝不打断主流程"""
    try:
        with _lock:
            t = _tasks.get(base)
            stages, details = {}, {}
            for k, st in (t["stages"].items() if t else []):
                s = {"state": st.get("state", "pending")}
                if st.get("start"):
                    s["start"] = st["start"]
                if st.get("end"):
                    s["end"] = st["end"]
                if st.get("start") and st.get("end"):
                    s["duration"] = round(st["end"] - st["start"], 1)
                s["detail"] = st.get("detail", "")
                stages[k] = s
                details[k] = st.get("detail", "")
        d = dec or {}
        fi = FONTS.get(d.get("font", "雅黑"), FONTS["雅黑"])
        rec = {"name": base, "state": state,
               "finished_at": time.time(), "started_at": t0,
               "duration_s": round(time.time() - t0, 1),
               "verdict": verdict, "output": output,
               "decisions": {
                   "interp": bool(interp_on),
                   "interp_detail": details.get("interp", ""),
                   "bilingual": bool(bilingual),
                   "font": d.get("font", "雅黑"),
                   "font_family": fi["family"],
                   "bgm": d.get("bgm"),
                   "bgm_type": bgm_type,
                   "mood": d.get("mood"),
                   "bgm_track": bgm_track,
                   "bgm_manual": bgm_manual,
                   "voice_manual": voice_manual,
                   "voice": voice,
                   "qc_detail": details.get("qc", "")},
               "stages": stages}
        if error:
            rec["error"] = str(error)[:200]
        _append_history(rec)
    except Exception:
        pass


def _pct_of(t):
    """整体进度：已完成阶段按自适应权重计满，活跃阶段计 0（前端按时钟插值）"""
    w = {k: max(_eta.get(k, 30), 5) for k in STAGE_KEYS}
    total = sum(w.values())
    done = sum(w[k] for k in STAGE_KEYS
               if t["stages"].get(k, {}).get("state") in ("done", "skip", "fail"))
    return round(done / total * 100)


def task_create(base, pos):
    with _lock:
        _tasks[base] = {"name": base, "state": "queued", "pos": pos,
                        "queued_at": time.time(), "started_at": None,
                        "ended_at": None, "cur": None, "pct": 0,
                        "verdict": None, "output": None, "error": None,
                        "bgm_manual": "off",
                        "voice_manual": "off", "voice": None,
                        "stages": {k: {"state": "pending", "start": None,
                                       "end": None, "detail": ""}
                                   for k in STAGE_KEYS}}
    ev(base, None, f"入列 · 排队第 {pos} 位")


def stage_begin(base, key, detail=""):
    with _lock:
        t = _tasks.get(base)
        if not t:
            return
        t["cur"] = key
        t["stages"][key] = {"state": "active", "start": time.time(),
                            "end": None, "detail": detail}
        t["pct"] = _pct_of(t)
    ev(base, key, detail or "开始")


def stage_detail(base, key, detail):
    with _lock:
        t = _tasks.get(base)
        if not t:
            return
        st = t["stages"].get(key)
        if st and st["state"] == "active":
            st["detail"] = detail
    ev(base, key, detail)


def stage_end(base, key, state="done", detail=""):
    """state: done | skip | fail"""
    with _lock:
        t = _tasks.get(base)
        if not t:
            return
        st = t["stages"].get(key) or {"start": time.time()}
        st["state"] = state
        st["end"] = time.time()
        if detail:
            st["detail"] = detail
        t["pct"] = _pct_of(t)
        if st.get("start") and state == "done":
            actual = st["end"] - st["start"]
            old = _eta.get(key, STAGE_DEFAULT.get(key, 60))
            _eta[key] = round(old * 0.5 + actual * 0.5, 1)
    if state == "skip" and detail:
        ev(base, key, detail)


def record_last_result(base, font_family, decision, verdict, minutes):
    global _last_result
    d = decision or {}
    _last_result = {"file": base, "font": font_family,
                    "bgm": d.get("bgm"), "mood": d.get("mood"),
                    "verdict": verdict, "minutes": minutes,
                    "reasons": d.get("reasons", {})}
    _save_eta()


# ═══════════════ 长片（film）编排引擎：状态机 + ComfyUI 驱动 + 拼接 ═══════════════

def _film_pct(f):
    """长片总进度：决策 6% + 逐段 76%（按段内子状态折算）+ 拼接/配乐/交付 各 6%"""
    if f["state"] == "done":
        return 100
    if f["state"] == "queued":
        return 0
    base = {"decide": 2, "generate": 6, "concat": 82, "voice": 86,
            "music": 90, "deliver": 95}
    p = base.get(f["phase"], 0)
    if f["phase"] == "generate" and f["segments"]:
        sub = {"pending": 0.0, "gen": 0.3, "sub": 0.6, "qc": 0.85, "fail": 1.0}
        frac = sum(sub.get(s["state"], 1.0) for s in f["segments"]) / len(f["segments"])
        p = 6 + int(76 * frac)
    return min(int(p), 99)


def film_create(name, pos):
    with _lock:
        _films[name] = {"name": name, "state": "queued", "pos": pos,
                        "queued_at": time.time(), "started_at": None,
                        "ended_at": None, "phase": None,
                        "phase_text": "排队中", "pct": 0, "error": None,
                        "output": None, "segments": [],
                        "need_storyboard": None, "style_anchor": None,
                        "note": "", "voice": None}
    ev(name + ".film", None, f"长片入列 · 排队第 {pos} 位")


def film_set(name, **kw):
    """更新长片任务字段并重算进度；任务不存在则忽略"""
    with _lock:
        f = _films.get(name)
        if not f:
            return
        f.update(kw)
        f["pct"] = _film_pct(f)


def film_seg_set(name, idx, **kw):
    """更新某段状态（idx 原位，顺序永不改变）"""
    with _lock:
        f = _films.get(name)
        if not f or not (0 <= idx < len(f["segments"])):
            return
        f["segments"][idx].update(kw)
        f["pct"] = _film_pct(f)


def _worst_verdict(verdicts):
    """段 verdict 聚合成整片结论：broken > flawed > good（'?' 评分未产出不计）"""
    vs = [v for v in verdicts if v in ("good", "flawed", "broken")]
    if not vs:
        return "?"
    return max(vs, key=lambda v: {"good": 0, "flawed": 1, "broken": 2}[v])


def _film_safe(name):
    """片名 → 可进文件名的安全串（SaveVideo 前缀 / input 图片名）"""
    return re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff\-]+", "_", name)[:60] or "film"


def _sha256(path):
    """文件指纹：音色阶段前后比子视频，证明未改动（读失败返回空串）"""
    try:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        return h.hexdigest()
    except OSError:
        return ""


def comfy_post_prompt(graph):
    """POST /prompt（API 格式工作流）；返回 prompt_id。HTTP 错误带摘要抛出"""
    import urllib.request
    import urllib.error
    data = json.dumps({"prompt": graph, "client_id": "h3-film-driver"}).encode("utf-8")
    req = urllib.request.Request(COMFY + "/prompt", data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            body = ""
        raise RuntimeError(f"ComfyUI 拒绝工作流 HTTP {e.code}: {body}")
    pid = d.get("prompt_id")
    if not pid:
        raise RuntimeError("ComfyUI 未返回 prompt_id")
    return pid


def comfy_wait_prompt(prompt_id, timeout=FILM_GEN_TIMEOUT, tick=None):
    """轮询 GET /history/{prompt_id}：completed → 返回 entry；error → 抛错；超时 → 抛错"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib_get(f"{COMFY}/history/{prompt_id}", timeout=10) as r:
                h = json.load(r)
            entry = h.get(prompt_id) if isinstance(h, dict) else None
            if entry:
                st = entry.get("status") or {}
                if st.get("status_str") == "error":
                    msg = ""
                    for m in (st.get("messages") or []):
                        if isinstance(m, (list, tuple)) and m and m[0] == "execution_error":
                            msg = json.dumps(m[1], ensure_ascii=False)[:200]
                            break
                    raise RuntimeError("ComfyUI 段执行失败: " + (msg or "未知错误"))
                if st.get("completed"):
                    return entry
        except RuntimeError:
            raise
        except Exception:
            pass  # 单次查询失败继续轮询
        if tick:
            try:
                tick()
            except Exception:
                pass
        time.sleep(POLL)
    raise RuntimeError(f"ComfyUI 段生成超时（>{timeout}s）")


def comfy_output_video(entry):
    """从 history entry.outputs 找 mp4 产物 → RAW（--output-directory）下绝对路径"""
    for node_outs in (entry.get("outputs") or {}).values():
        for g in (node_outs or []):
            fn = (g or {}).get("filename") or ""
            if fn.lower().endswith(".mp4"):
                return os.path.join(RAW, (g.get("subfolder") or ""), fn)
    return None


def comfy_wait_idle(timeout=900):
    """等 ComfyUI 空闲再 POST：查询失败(None)当『未知』继续等，绝不当空闲；
    连续 5 分钟不可达或忙碌超 timeout 则抛错（防与 UI 手动任务叠加打爆显存）"""
    t0, last_ok = time.time(), time.time()
    while time.time() - t0 < timeout:
        b = comfy_busy()
        if b is False:
            return
        if b is None:
            if time.time() - last_ok > 300:
                raise RuntimeError("ComfyUI 队列查询连续 5 分钟失败（服务器不可达）")
        else:
            last_ok = time.time()
        time.sleep(5)
    raise RuntimeError("ComfyUI 忙碌超时，无法开始段生成")


def film_last_frame(video, png):
    """抽段尾种子帧：-sseof -0.15 回退 0.15s 取仍有动势的帧（避开减速死尾，调研实测命令）"""
    r = subprocess.run([FFMPEG, "-y", "-sseof", "-0.15", "-i", video,
                        "-update", "1", "-frames:v", "1", png],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0 or not os.path.exists(png):
        raise RuntimeError("抽取段尾帧失败: " + (r.stderr or "")[-150:])
    return png


def film_resolve_reference(name, ref):
    """首段参考图：绝对路径原样；文件名依次找 RAW / ComfyUI input / OUT；复制进 input 目录"""
    cands = ([ref] if ref and os.path.isabs(ref)
             else [os.path.join(RAW, ref), os.path.join(COMFY_INPUT, ref),
                   os.path.join(OUT, ref)]) if ref else []
    src = next((c for c in cands if c and os.path.isfile(c)), None)
    if not src:
        raise RuntimeError("首段参考图未找到: " + str(ref))
    dst = os.path.join(COMFY_INPUT, "film_" + _film_safe(name) + "_ref.png")
    shutil.copy2(src, dst)
    return os.path.basename(dst)


def film_generate_segment(name, idx, seg_prompt, seed, ref_name, workdir):
    """驱动 ComfyUI 生成一段：改模板参数 → 等空闲 → POST → 等 history → 取产物落 workdir。
    按段替换的节点：114 LoadImage 首帧 / 104 提示词 / 15 种子 / 111 时长秒 / 92 文件前缀"""
    with open(FILM_TEMPLATE, encoding="utf-8") as f:
        graph = json.load(f)
    graph["114"]["inputs"]["image"] = ref_name
    graph["104"]["inputs"]["prompt"] = seg_prompt
    graph["15"]["inputs"]["noise_seed"] = seed
    graph["111"]["inputs"]["value"] = FILM_SEG_SEC
    graph["92"]["inputs"]["filename_prefix"] = FILM_SEG_PREFIX + _film_safe(name) + f"_s{idx:02d}"
    comfy_wait_idle()
    pid = comfy_post_prompt(graph)
    entry = comfy_wait_prompt(pid)
    src = comfy_output_video(entry)
    if not src or not os.path.isfile(src):
        cands = glob.glob(os.path.join(RAW, FILM_SEG_PREFIX + _film_safe(name)
                                       + f"_s{idx:02d}*.mp4"))
        src = max(cands, key=os.path.getmtime) if cands else None
    if not src or not os.path.isfile(src):
        raise RuntimeError(f"seg_{idx:02d} 生成无产物文件（prompt_id={pid}）")
    prev = os.path.getsize(src)  # 尺寸双检，防读到半写文件
    time.sleep(2)
    if os.path.getsize(src) != prev:
        time.sleep(4)
    dst = os.path.join(workdir, f"seg_{idx:02d}_raw.mp4")
    shutil.copy2(src, dst)
    try:  # 段产物由编排器独占消费，移出 raw 防堆积（detector 亦按前缀跳过，双保险）
        os.remove(src)
    except OSError:
        pass
    return {"prompt_id": pid, "file": dst}


def film_probe_fps(video):
    r = subprocess.run([FFMPEG, "-i", video], capture_output=True,
                       text=True, creationflags=NO_WINDOW)
    m = re.search(r"Video:.*?([\d.]+)\s*fps", r.stderr or "")
    return float(m.group(1)) if m else 0.0


def probe_audio(path):
    """音频参数指纹 '采样率:声道'（无音轨/探测失败返回 None）——concat -c copy 需全段一致"""
    try:
        r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True,
                           creationflags=NO_WINDOW)
    except Exception:
        return None
    m = re.search(r"(\d+) Hz, (mono|stereo)", r.stderr or "")
    if not m:
        return None
    return f"{m.group(1)}:{1 if m.group(2) == 'mono' else 2}"


VOICE_ENHANCE_FILTER = (
    "highpass=f=75,lowpass=f=12000,"
    "afftdn=nf=-25,"
    "acompressor=threshold=-18dB:ratio=3:attack=5:release=80:makeup=2,"
    "equalizer=f=3000:t=q:w=1:g=1.5,"
    "loudnorm=I=-16:TP=-1.5:LRA=11"
)


def _video_packet_count(video):
    """数视频流 packet（framemd5 一行一个）；失败返回 0。"""
    r = subprocess.run([FFMPEG, "-v", "error", "-i", video, "-map", "0:v:0",
                        "-c", "copy", "-f", "framemd5", "-"],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        return 0
    return sum(1 for ln in (r.stdout or "").splitlines()
               if ln and not ln.startswith("#"))


def enhance_voice_audio(path):
    """人声增强 + 响度归一（-16 LUFS）；只重编码音频，视频流完整 copy。
    音频用 atrim 对齐到视频时长，禁用 -shortest，绝不裁掉视频帧。
    返回处理后的原路径；无音轨/失败/输出帧数变少返回 None（调用方继续原流程）。
    每段都做，可消除 H3 段间 ~3dB 电平差，并让 concat -c copy 保持 48k 立体声。"""
    if not os.path.isfile(path) or probe_audio(path) is None:
        return None
    vdur = probe_duration(path)
    if vdur <= 0:
        return None
    # 音轨用 atrim 对齐到成片时长：长于视频时截断，短于视频时保持原长；
    # 禁用 -shortest，绝不让 muxer 因音轨提前结束而丢掉最后一帧。
    afilter = (VOICE_ENHANCE_FILTER
               + f",atrim=start=0:end={vdur:.6f},asetpts=PTS-STARTPTS")
    src_n = _video_packet_count(path)
    tmp = os.path.join(os.path.dirname(path),
                       os.path.splitext(os.path.basename(path))[0] + "_voice.mp4")
    cmd = [FFMPEG, "-y", "-i", path, "-map", "0:v:0", "-map", "0:a:0",
           "-c:v", "copy", "-af", afilter,
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    out_n = (_video_packet_count(tmp)
             if r.returncode == 0 and os.path.isfile(tmp) else 0)
    if (r.returncode != 0 or not os.path.isfile(tmp)
            or bool(src_n and out_n and out_n < src_n)):
        try:
            os.remove(tmp)
        except OSError:
            pass
        if src_n and out_n and out_n < src_n:
            log(f"[audio] 音色增强丢帧（{src_n}→{out_n}），保留原音轨: {path}")
        else:
            log(f"[audio] 音色增强失败，保留原音轨: {(r.stderr or '')[-160:]}")
        return None
    os.replace(tmp, path)
    return path



def film_probe_uniform(files):
    """段间一致性校验：分辨率一致（同模板产出帧率/采样率天然一致）→ 可 -c copy"""
    dims = [probe_media(p) for p in files]
    return len(set(dims)) == 1, dims, [round(probe_duration(p), 2) for p in files]


def film_concat_copy(files, out):
    """全硬切且参数一致：concat demuxer + -c copy（秒级、零画质损失）"""
    lst = os.path.splitext(out)[0] + "_list.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in files:
            f.write("file '" + os.path.basename(p).replace("'", "'\\''") + "'\n")
    r = subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i",
                        os.path.basename(lst), "-c", "copy", os.path.basename(out)],
                       cwd=os.path.dirname(out), capture_output=True, text=True,
                       creationflags=NO_WINDOW)
    ok = r.returncode == 0 and os.path.isfile(out)
    try:
        if ok:
            os.remove(lst)
    except OSError:
        pass
    if not ok:
        raise RuntimeError("concat -c copy 拼接失败: " + (r.stderr or "")[-200:])
    return out


def film_normalize(files, workdir):
    """参数不一致 → 统一重编码（第一段的分辨率/帧率为基准）再拼，避免 demuxer 直接报错"""
    w, h = probe_media(files[0])
    fps = film_probe_fps(files[0]) or 24
    out = []
    for i, p in enumerate(files):
        dst = os.path.join(workdir, f"norm_{i:02d}.mp4")
        # fps 滤镜只在源帧率确实不同时启用：同帧率再加 fps=N 会因时间戳
        # 舍入丢掉末帧（实测 24fps 243 帧 → 242）；passthrough 禁止任何隐式补/丢帧
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        src_fps = film_probe_fps(p)
        if src_fps and abs(src_fps - fps) > 0.01:
            vf += f",fps={fps}"
        r = subprocess.run(
            [FFMPEG, "-y", "-i", p, "-vf", vf, "-fps_mode", "passthrough",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-profile:v", "main", "-level", "4.0",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", dst],
            capture_output=True, text=True, creationflags=NO_WINDOW)
        if r.returncode != 0 or not os.path.isfile(dst):
            raise RuntimeError(f"seg_{i:02d} 归一化重编码失败: " + (r.stderr or "")[-200:])
        out.append(dst)
    return out


def film_concat_xfade(files, transitions, out, dur=0.5):
    """含 crossfade：xfade 链 + acrossfade 全片重编码（无 -c copy 版本）。
    offset_n = 前一 offset + 新段实际时长 − duration（调研公式，逐段实测时长计算）"""
    durs = [probe_duration(p) for p in files]
    vf, af = [], []
    prev_v, prev_a = "0:v", "0:a"  # 内部记裸标签，引用时再包 []，防链式 [[xv1]] 双括号
    offset = durs[0] - dur
    for i in range(1, len(files)):
        vo, ao = f"xv{i}", f"xa{i}"
        vf.append(f"[{prev_v}][{i}:v]xfade=transition=fade:duration={dur}"
                  f":offset={offset:.3f}[{vo}]")
        af.append(f"[{prev_a}][{i}:a]acrossfade=d={dur}[{ao}]")
        prev_v, prev_a = vo, ao
        if i + 1 < len(files):
            offset = offset + durs[i] - dur
    cmd = [FFMPEG, "-y"]
    for p in files:
        cmd += ["-i", p]
    cmd += ["-filter_complex", ";".join(vf + af),
            "-map", f"[{prev_v}]", "-map", f"[{prev_a}]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-profile:v", "main", "-level", "4.0",
            "-c:a", "aac", "-b:a", "192k", out]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0 or not os.path.isfile(out):
        raise RuntimeError("xfade 拼接失败: " + (r.stderr or "")[-200:])
    return out


HTML = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>H3 导演台 · Director's Desk</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0d0d11; --panel:#15151b; --raise:#1a1a22; --line:#26262e;
  --ink:#e8e4dc; --mut:#8e8a80; --dim:#5a574e;
  --amber:#e2a33c; --amber-hi:#f2b950; --amber-dim:rgba(226,163,60,.14);
  --sage:#7fb37a; --danger:#d96c5f; --hold:#b3a266;
}
html,body{height:100%}
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink);
     display:flex;flex-direction:column;overflow:hidden;user-select:none}
/* 胶片颗粒 */
body::after{content:"";position:fixed;inset:0;pointer-events:none;opacity:.045;z-index:90;
 background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)' opacity='0.55'/%3E%3C/svg%3E")}
/* —— 自定义滚动条（全站替换 Windows 默认样式）—— */
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:rgba(255,255,255,.02);border-radius:8px}
::-webkit-scrollbar-thumb{background:#2e2e38;border-radius:8px;border:2px solid transparent;
  background-clip:padding-box;transition:background .2s}
::-webkit-scrollbar-thumb:hover{background:#4a4a56;background-clip:padding-box}
::-webkit-scrollbar-corner{background:transparent}
*{scrollbar-width:thin;scrollbar-color:#2e2e38 rgba(255,255,255,.02)}

header{display:flex;align-items:baseline;gap:14px;padding:16px 24px 12px;flex:none;
  border-bottom:1px solid #000;box-shadow:0 1px 0 var(--line),0 4px 14px rgba(0,0,0,.35)}
.brand{font-size:16px;font-weight:700;letter-spacing:1px;display:flex;align-items:center;gap:10px}
.brand svg{width:19px;height:19px;color:var(--amber)}
.brand small{font-size:10px;opacity:.38;margin-left:4px;letter-spacing:3px;font-weight:600}
#hstat{margin-left:auto;font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}
#hstat b{color:var(--amber-hi);font-weight:600}
#board{flex:1;overflow-y:auto;padding:16px 24px 28px}
#board.grabbing{cursor:grabbing}
.empty{color:var(--dim);font-size:13px;text-align:center;padding:90px 0;line-height:2.2}
.empty svg{width:34px;height:34px;opacity:.4;display:block;margin:0 auto 10px}
.empty b{color:var(--mut);font-weight:600}

.lane{background:linear-gradient(180deg,var(--panel),#131318);border:1px solid var(--line);
  border-radius:10px;padding:13px 16px 11px;margin-bottom:12px;position:relative;
  animation:laneIn .55s cubic-bezier(.22,1,.36,1) both}
@keyframes laneIn{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
.lane-head{display:flex;flex-wrap:nowrap;align-items:center;gap:10px;margin-bottom:11px}
.fname{font-size:13px;font-weight:600;flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip{font-size:10px;padding:2.5px 10px;border-radius:3px;letter-spacing:2px;font-weight:600;
  white-space:nowrap;flex:none;transition:background .3s,color .3s,border-color .3s}
.c-queued{background:#22222a;color:var(--mut)}
.c-run{background:var(--amber-dim);color:var(--amber-hi);border:1px solid rgba(226,163,60,.35)}
.c-done{background:rgba(127,179,122,.1);color:var(--sage);border:1px solid rgba(127,179,122,.3)}
.c-err{background:rgba(217,108,95,.1);color:var(--danger);border:1px solid rgba(217,108,95,.35)}
.lmeta{margin-left:auto;font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums;flex:none;
  display:flex;align-items:center;gap:12px}
.lmeta .pct{color:var(--ink);font-weight:700}
.dlbtn{display:none;align-items:center;gap:6px;font-size:11px;font-weight:600;color:var(--amber-hi);flex:none;
  background:var(--amber-dim);border:1px solid rgba(226,163,60,.4);border-radius:5px;
  padding:4px 10px;cursor:pointer;letter-spacing:1px;text-decoration:none;
  transition:all .2s cubic-bezier(.22,1,.36,1)}
.dlbtn svg{width:12px;height:12px}
.dlbtn:hover{background:rgba(226,163,60,.24);transform:translateY(-1px);
  box-shadow:0 4px 12px rgba(226,163,60,.2)}
.lane.done .dlbtn{display:inline-flex}

.rail-wrap{overflow-x:auto;overflow-y:hidden;scrollbar-width:thin;cursor:grab;padding:2px 0}
.rail-wrap.grabbing{cursor:grabbing}
.rail{display:flex;align-items:flex-start;min-width:max-content;padding:6px 2px 2px}
.stage{width:104px;flex:none;text-align:center;position:relative}
.nwrap{position:relative;width:34px;margin:0 auto}
.ring{position:absolute;inset:-8px;opacity:0;pointer-events:none;background-repeat:no-repeat;
 background-image:
  linear-gradient(var(--amber),var(--amber)),linear-gradient(var(--amber),var(--amber)),
  linear-gradient(var(--amber),var(--amber)),linear-gradient(var(--amber),var(--amber)),
  linear-gradient(var(--amber),var(--amber)),linear-gradient(var(--amber),var(--amber)),
  linear-gradient(var(--amber),var(--amber)),linear-gradient(var(--amber),var(--amber));
 background-position:0 0,0 0,100% 0,100% 0,0 100%,0 100%,100% 100%,100% 100%;
 background-size:12px 2px,2px 12px,12px 2px,2px 12px,12px 2px,2px 12px,12px 2px,2px 12px;
 filter:drop-shadow(0 0 5px rgba(226,163,60,.55))}
.s-active .ring{opacity:1;animation:cornersIn .5s cubic-bezier(.22,1,.36,1),breathe 2.2s ease-in-out .5s infinite}
@keyframes cornersIn{from{transform:scale(1.35);opacity:0}to{transform:scale(1);opacity:1}}
@keyframes breathe{0%,100%{opacity:.85}50%{opacity:.45}}
.node{width:34px;height:34px;border-radius:50%;border:1.5px solid #3a3a44;background:#111117;
  display:flex;align-items:center;justify-content:center;font-size:13px;
  color:var(--dim);font-weight:600;position:relative;z-index:2;transition:all .3s}
.s-pending .node{color:var(--dim)}
.s-active .node{border-color:var(--amber);color:var(--amber-hi);
  box-shadow:0 0 16px rgba(226,163,60,.35);animation:pulse 1.8s infinite}
.s-done .node{border-color:var(--sage);color:var(--sage);background:rgba(127,179,122,.08)}
.s-skip .node{border-style:dashed;border-color:var(--hold);color:var(--hold)}
.s-fail .node{border-color:var(--danger);color:var(--danger);background:rgba(217,108,95,.08)}
@keyframes pulse{0%,100%{box-shadow:0 0 5px rgba(226,163,60,.2)}50%{box-shadow:0 0 18px rgba(226,163,60,.55)}}
.conn{height:2px;flex:1;background:var(--line);margin-top:22px;min-width:16px;border-radius:1px;
  position:relative;overflow:hidden}
.conn.on{background:linear-gradient(90deg,var(--sage),var(--amber))}
.conn.flow{background:repeating-linear-gradient(90deg,var(--amber) 0 6px,transparent 6px 13px);
  animation:flow .9s linear infinite}
@keyframes flow{to{background-position:13px 0}}
.sname{font-size:12px;font-weight:600;margin-top:8px;color:#cfcabf;transition:color .3s}
.s-pending .sname{color:var(--dim)}
.ssub{font-size:10px;opacity:.4;margin-top:2px}
.sbar{height:3px;background:rgba(0,0,0,.5);border-radius:2px;margin:7px 10px 0;overflow:hidden;position:relative}
.sbar div{height:100%;width:0;background:linear-gradient(90deg,#b97f2e,var(--amber));
  transition:width .5s linear;border-radius:2px;position:relative}
.s-active .sbar div::after{content:"";position:absolute;inset:0;
  background:linear-gradient(105deg,transparent 30%,rgba(255,236,200,.45) 50%,transparent 70%);
  background-size:220% 100%;animation:shimmer 1.7s linear infinite}
@keyframes shimmer{from{background-position:130% 0}to{background-position:-90% 0}}
.smeta{font-size:10px;color:var(--mut);margin-top:5px;font-variant-numeric:tabular-nums;min-height:13px}
.curline{font-size:11px;color:var(--mut);margin:9px 2px 7px;min-height:15px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.curline b{color:var(--amber-hi);font-weight:600}
.curline a{color:var(--amber-hi);text-decoration:none}
.fnote{display:none;font-size:11px;color:var(--amber-hi);margin:0 2px 6px;white-space:nowrap;
  text-overflow:ellipsis;overflow:hidden}
.logbox{position:relative;background:#0b0b0f;border:1px solid #202028;border-radius:7px;height:96px;
  overflow-y:auto;padding:6px 10px;font:10.5px/1.75 Consolas,"Microsoft YaHei",monospace}
.ll{white-space:nowrap;animation:logIn .3s ease both}
@keyframes logIn{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:none}}
.lt{color:#4a4a56;margin-right:8px}
.ls{color:var(--amber);opacity:.8;margin-right:8px}
.lx{color:#9a96a0}
.pill{position:absolute;right:14px;bottom:14px;z-index:5;display:none;align-items:center;gap:6px;
  font-size:11px;font-weight:600;color:#111;background:var(--amber);border-radius:20px;
  padding:4px 12px;cursor:pointer;border:none;box-shadow:0 4px 14px rgba(226,163,60,.4);
  transition:transform .2s}
.pill:hover{transform:translateY(-1px)}
.pill.show{display:inline-flex;animation:pillIn .25s cubic-bezier(.22,1,.36,1)}
@keyframes pillIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}

/* —— 工具条：视图切换 / 搜索 / 配乐总开关 —— */
#toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 24px;flex:none;
  border-bottom:1px solid var(--line);background:rgba(21,21,27,.6)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden;background:#111117}
.seg-btn{appearance:none;border:none;background:none;color:var(--mut);font-size:12px;
  font-weight:600;letter-spacing:1px;padding:6px 15px;cursor:pointer;transition:all .2s}
.seg-btn+.seg-btn{border-left:1px solid var(--line)}
.seg-btn:hover{color:var(--ink)}
.seg-btn.active{background:var(--amber-dim);color:var(--amber-hi)}
#q{flex:1;min-width:120px;max-width:360px;background:#0f0f14;border:1px solid var(--line);
  border-radius:6px;color:var(--ink);font-size:12px;padding:6px 11px;outline:none;transition:border-color .2s}
#q::placeholder{color:var(--dim)}
#q:focus{border-color:rgba(226,163,60,.5)}
/* 配乐本片把控：三态（自动=听 JEV / 关=本片不用 / 开=本片必配） */
.bgmsw{appearance:none;border-radius:5px;font-size:10px;font-weight:600;letter-spacing:1px;
  padding:3px 9px;cursor:pointer;transition:all .2s;flex:none}
.bgmsw.auto{background:#22222a;color:var(--mut);border:1px solid var(--line)}
.bgmsw.off{background:rgba(217,108,95,.1);color:var(--danger);border:1px solid rgba(217,108,95,.4)}
.bgmsw.on{background:var(--amber-dim);color:var(--amber-hi);border:1px solid rgba(226,163,60,.45)}
.bgmsw:hover{filter:brightness(1.18)}
.bgmsw:disabled{opacity:.55;cursor:not-allowed}
.bgmsw.busy{cursor:wait}

/* —— 已完成 / 搜索视图：紧凑卡片列表 —— */
.listview{flex:1;overflow-y:auto;padding:14px 24px 28px;display:none}
.dcard{background:linear-gradient(180deg,var(--panel),#131318);border:1px solid var(--line);
  border-radius:9px;padding:11px 15px;margin-bottom:9px}
.dcard-head{display:flex;flex-wrap:nowrap;align-items:center;gap:10px;min-width:0}
.dname{font-size:13px;font-weight:600;flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ddate{font-size:11px;color:var(--mut);font-variant-numeric:tabular-nums;white-space:nowrap;flex:none}
.ddec{font-size:11px;color:var(--mut);margin-top:6px;line-height:1.8;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.derr{font-size:11px;color:var(--danger);margin-top:5px;word-break:break-all}
.dfoot{margin-top:6px}
.dpath{font-size:11px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;display:block}
.dcard .dlbtn{display:inline-flex;flex:none}
/* 删除按钮：内联 SVG 垃圾桶，悬停才转红，避免误触 */
.delbtn{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;flex:none;
  padding:0;background:transparent;border:1px solid transparent;border-radius:5px;color:var(--mut);
  cursor:pointer;transition:all .2s cubic-bezier(.22,1,.36,1)}
.delbtn svg{width:13px;height:13px;display:block}
.delbtn:hover{color:var(--danger);background:rgba(217,108,95,.12);
  border-color:rgba(217,108,95,.4)}
.delbtn:focus-visible{outline:1px solid var(--danger);outline-offset:1px}
.delbtn.busy,.delbtn:disabled{opacity:.5;cursor:wait}

/* —— 长片（film）：可折叠长片卡 + seg_XX 子项行 —— */
.flane{background:linear-gradient(180deg,var(--panel),#131318);border:1px solid var(--line);
  border-radius:10px;padding:13px 16px 11px;margin-bottom:12px;position:relative;
  animation:laneIn .55s cubic-bezier(.22,1,.36,1) both}
.flane .dlbtn{display:none}
.flane.done .dlbtn{display:inline-flex}
.flane .lane-head{cursor:pointer;user-select:none}
.fcaret{font-size:10px;color:var(--mut);flex:none;transition:transform .25s;display:inline-block}
.flane.open .fcaret,.dcard.open .fcaret{transform:rotate(90deg)}
.fsegs{display:none;margin-top:6px;border-top:1px dashed var(--line);padding-top:7px}
.flane.open .fsegs,.dcard.open .fsegs{display:block}
.fseg{display:flex;align-items:center;gap:10px;padding:3.5px 2px;font-size:12px;color:var(--mut)}
.fseg .sdot{width:8px;height:8px;border-radius:50%;background:#3a3a44;flex:none;transition:background .3s}
.fseg.s-gen .sdot{background:var(--amber);box-shadow:0 0 8px rgba(226,163,60,.6);animation:pulse 1.8s infinite}
.fseg.s-sub .sdot{background:var(--amber-dim);border:1px solid var(--amber)}
.fseg.s-qc .sdot{background:var(--hold)}
.fseg.s-done .sdot{background:var(--sage)}
.fseg.s-fail .sdot{background:var(--danger)}
.fseg .sname{color:var(--ink);font-weight:600;min-width:66px;font-variant-numeric:tabular-nums}
.fseg .sret{font-size:11px;color:var(--hold);min-width:44px}
.fseg .sv{font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 auto}
.fseg .sv .v-good{color:var(--sage)} .fseg .sv .v-bad{color:var(--danger)}
.fseg .sdl{font-size:11px;font-weight:600;color:var(--amber-hi);text-decoration:none;
  flex:none;display:none;letter-spacing:1px}
.fseg.s-done .sdl{display:inline}
.fseg.s-done .sdl:hover{color:var(--amber)}
.dcard .fcaret{cursor:pointer}
.dcard .ddec{cursor:default}

/* 嵌入模式（ComfyUI 抽屉 iframe） */
body.embed header{padding:9px 14px 7px}
body.embed .brand,body.embed .brand small{display:none}
body.embed #board{padding:10px 14px 18px}
body.embed #toolbar{padding:6px 14px;gap:8px}
body.embed .seg-btn{font-size:11px;padding:4px 11px}
body.embed #q{font-size:11px;padding:4px 9px;max-width:230px;min-width:90px}
body.embed .listview{padding:10px 14px 18px}
body.embed .dcard{padding:9px 12px;margin-bottom:7px}
body.embed .lane{padding:10px 12px 8px;margin-bottom:8px}
body.embed .logbox{height:64px}
body.embed .stage{width:90px}
body.embed .lane-head{gap:6px}
body.embed .chip{padding:2.5px 7px}

@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}
}
</style></head><body>
<header>
  <span class="brand">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3Z"/><path d="m6.2 5.3 3.1 3.9"/><path d="m12.4 3.4 3.1 4"/><path d="M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>
    H3 导演台<small>DIRECTOR'S DESK</small>
  </span>
  <span id="hstat">连接中…</span>
</header>
<div id="toolbar">
  <div class="seg">
    <button class="seg-btn active" id="segRun">制作中</button>
    <button class="seg-btn" id="segDone">已完成</button>
  </div>
  <input id="q" type="text" placeholder="搜索：日期如 1017 / 主题如 女友" autocomplete="off" spellcheck="false">
</div>
<main id="board"><div id="empty" class="empty">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3Z"/><path d="m6.2 5.3 3.1 3.9"/><path d="m12.4 3.4 3.1 4"/><path d="M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>
  待片中<br>将成片放入 <b>H:\h3cache\raw</b> · 出片后自动开工</div></main>
<main id="doneList" class="listview"></main>
<main id="searchList" class="listview"></main>
<script>
"use strict";
const EMBED=new URLSearchParams(location.search).has("embed");
if(EMBED)document.body.classList.add("embed");
const board=document.getElementById("board");
const emptyEl=document.getElementById("empty");
const doneList=document.getElementById("doneList");
const searchList=document.getElementById("searchList");
const qInput=document.getElementById("q");
const segRun=document.getElementById("segRun");
const segDone=document.getElementById("segDone");
const lanes=new Map();
let DATA=null,view="run",doneSig="",searchSig="";
const DL_SVG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></svg>';
const DEL_SVG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>';
async function deleteRecord(name,btn){
  if(!btn||btn.disabled)return;
  if(!confirm("删除「"+name+"」及其磁盘产物？此操作不可恢复"))return;
  btn.disabled=true;btn.classList.add("busy");
  try{
    const r=await fetch("/task/delete",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name:name})});
    let d={};try{d=await r.json()}catch(_){}
    if(!r.ok||!d.ok)throw new Error(d.error||("HTTP "+r.status));
    if(DATA&&Array.isArray(DATA.history))
      DATA.history=DATA.history.filter(x=>x.name!==name);
    doneSig="";searchSig="";
    renderViews();
  }catch(e){
    alert("删除失败："+(e&&e.message?e.message:e));
    btn.disabled=false;btn.classList.remove("busy");
  }
}
function delBtnHtml(){
  return `<button class="delbtn" type="button" title="删除记录与磁盘产物" aria-label="删除">${DEL_SVG}</button>`;
}
function bindDelBtn(el,name){
  const b=el.querySelector(".delbtn");if(!b)return;
  b.addEventListener("click",e=>{
    e.preventDefault();e.stopPropagation();
    deleteRecord(name,b);});
}

function esc(s){return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;")
  .replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function fmtT(s){s=Math.max(0,Math.round(s));const m=Math.floor(s/60);
  return m>0?m+"′"+String(s%60).padStart(2,"0")+"″":s+"″";}
function fmtClock(ts){return new Date(ts*1000).toTimeString().slice(0,8);}
function stageLabel(k,d){if(!k)return "调度";
  const s=(d.stages||[]).find(x=>x.key===k);return s?s.label:k;}

const CHIP={queued:["排队中","c-queued"],running:["制作中","c-run"],
            done:["已交付","c-done"],error:["异常终止","c-err"]};

/* —— 视图切换 / 搜索 / 配乐总开关 —— */
function fmtDate(ts){if(!ts)return "-";const x=new Date(ts*1000);
  return String(x.getMonth()+1).padStart(2,"0")+"-"+String(x.getDate()).padStart(2,"0")+" "+
    String(x.getHours()).padStart(2,"0")+":"+String(x.getMinutes()).padStart(2,"0");}
function ymd(ts){if(!ts)return "";const x=new Date(ts*1000);
  return ""+x.getFullYear()+String(x.getMonth()+1).padStart(2,"0")+String(x.getDate()).padStart(2,"0");}
function bgmTxt(dc){
  if(!dc)return "-";
  if(dc.bgm_manual==="off")return "手动关闭";
  if(dc.bgm_manual==="on")return "手动开启·"+(dc.mood||"-")+"·"+
    (dc.bgm_type==="vocal"?"有声":"轻音乐")+(dc.bgm_track?`(${dc.bgm_track})`:"");
  if(dc.bgm==="none")return "无";
  return (dc.mood||"-")+"·"+(dc.bgm_type==="vocal"?"有声":"轻音乐")+
    (dc.bgm_track?`(${dc.bgm_track})`:"");}
function decLine(r){const dc=r.decisions||{};
  const v=(dc.voice&&dc.voice.status)?(" · 音色 "+(dc.voice.status==="done"?"完成":
    dc.voice.status==="partial"?"部分":("跳过"+(dc.voice.reason?("("+dc.voice.reason+")"):"")))):"";
  return `补帧 ${dc.interp?"开":"关"} · 双语 ${dc.bilingual?"开":"关"} · `+
    `字体 ${esc(dc.font||"-")} · 配乐 ${esc(bgmTxt(dc))}${esc(v)} · 质检 ${esc(r.verdict||dc.qc_detail||"-")}`;}

function buildDoneCard(r){
  const el=document.createElement("div");el.className="dcard";
  const isErr=r.state==="error";
  const vbadge=isErr?"":`<span class="chip ${r.verdict==="good"?"c-done":"c-queued"}">质检 ${esc(r.verdict||"?")}</span>`;
  el.innerHTML=`<div class="dcard-head">
      <span class="dname" title="${esc(r.name)}">${esc(r.name)}</span>
      <span class="chip ${isErr?"c-err":"c-done"}">${isErr?"异常":"已交付"}</span>
      ${vbadge}
      <span class="ddate">${fmtDate(r.finished_at)} · 用时 ${fmtT(r.duration_s||0)}</span>
      <a class="dlbtn" href="/download?name=${encodeURIComponent(r.name)}">${DL_SVG}下载成片</a>
      ${delBtnHtml()}</div>
    <div class="ddec">${decLine(r)}</div>
    ${r.error?`<div class="derr">✗ ${esc(r.error)}</div>`:""}
    <div class="dfoot"><span class="dpath">保存于 ${esc(r.output||("H:\\h3cache\\"+r.name))}</span></div>`;
  bindDelBtn(el,r.name);
  return el;}

function buildTaskCard(t){
  const el=document.createElement("div");el.className="dcard";
  const [ctext,ccls]=CHIP[t.state]||[t.state,"c-queued"];
  const st=(t.cur&&t.stages)?t.stages[t.cur]:null;
  let info;
  if(t.state==="queued")info=t.pos===1?"排队待处理 · 即将开工":"排队待处理 · 队列第 "+(t.pos||"?")+" 位";
  else if(t.state==="running")info=stageLabel(t.cur,DATA)+" · "+((st&&st.detail)||"");
  else if(t.state==="done")info="成片 H:\\h3cache\\"+t.name+(t.verdict?" · 质检 "+t.verdict:"");
  else info=t.error||"处理异常";
  el.innerHTML=`<div class="dcard-head">
      <span class="dname" title="${esc(t.name)}">${esc(t.name)}</span>
      <span class="chip ${ccls}">${esc(ctext)}</span>
      <span class="ddate">${fmtDate(t.queued_at)}</span>
      ${t.state==="done"?`<a class="dlbtn" href="/download?name=${encodeURIComponent(t.name)}">${DL_SVG}下载成片</a>`:""}</div>
    <div class="ddec">${esc(info)}</div>`;
  return el;}

/* —— 长片（film）：搜索/已完成视图卡片（可折叠段子列表，与运行视图同款行） —— */
const FCHIP={queued:["排队中","c-queued"],running:["制作中","c-run"],
             done:["已交付","c-done"],error:["异常终止","c-err"]};
const FPHASE={decide:"分镜决策",generate:"逐段生成",concat:"拼接合成",
              voice:"音色转换",music:"整片配乐",deliver:"交付归档"};
const SEG_LABEL={pending:"待处理",gen:"生成中",sub:"字幕中",qc:"质检中",done:"完成",fail:"失败"};
const SEG_DOT={pending:"s-pending",gen:"s-gen",sub:"s-sub",qc:"s-qc",done:"s-done",fail:"s-fail"};
function segName(s){return "seg_"+String(s.idx).padStart(2,"0");}
function segRowsHtml(base,segs){
  return (segs||[]).map(s=>
    `<div class="fseg ${SEG_DOT[s.state]||"s-pending"}">`+
    `<span class="sdot"></span><span class="sname">${esc(segName(s))}</span>`+
    `<span class="sret">${s.retries?("重试"+s.retries):""}</span>`+
    `<span class="sv">${s.verdict?`<b class="${s.verdict==="good"?"v-good":"v-bad"}">质检 ${esc(s.verdict)}</b> `:""}${esc(s.note||SEG_LABEL[s.state]||s.state||"")}</span>`+
    (s.download?`<a class="sdl" href="/download?name=${encodeURIComponent(base+"/"+segName(s)+".mp4")}">下载</a>`:"")+
    `</div>`).join("");}
function filmDoneLine(r){
  const dc=r.decisions||{};
  return `长片 · 字体 ${dc.font||"-"} · 配乐 ${dc.bgm==="none"?"无":(dc.mood||"-")+"·"+(dc.bgm_type==="vocal"?"有声":"轻音乐")} · 拼接 ${dc.concat||"-"}`;}
function buildFilmCard(t){
  const el=document.createElement("div");el.className="dcard";
  const [ct,ccls]=FCHIP[t.state]||[t.state,"c-queued"];
  el.innerHTML=`<div class="dcard-head">
      <span class="fcaret">▶</span>
      <span class="dname" title="${esc(t.name)}.film">🎞 ${esc(t.name)}.film</span>
      <span class="chip ${ccls}">${esc(ct)}</span>
      <span class="ddate">${fmtDate(t.queued_at)} · 总进度 ${t.pct||0}%</span>
      ${t.state==="done"?`<a class="dlbtn" href="/download?name=${encodeURIComponent(t.name+"/"+t.name+".mp4")}">${DL_SVG}下载整片</a>`:""}</div>
    <div class="ddec">${esc((t.phase_label?"【"+t.phase_label+"】 ":"")+(t.phase_text||""))}</div>
    ${t.note?`<div class="ddec" style="color:var(--amber-hi)">${esc(t.note)}</div>`:""}
    ${t.error?`<div class="derr">✗ ${esc(t.error)}</div>`:""}
    <div class="fsegs">${segRowsHtml(t.name,t.segments)}</div>`;
  el.querySelector(".dcard-head").addEventListener("click",e=>{
    if(e.target.closest(".dlbtn"))return;
    el.classList.toggle("open");});
  return el;}
function buildFilmDoneCard(r){
  const el=document.createElement("div");el.className="dcard";
  const isErr=r.state==="error";
  const base=String(r.name||"").replace(/\.film$/,"");
  el.innerHTML=`<div class="dcard-head">
      <span class="fcaret">▶</span>
      <span class="dname" title="${esc(r.name)}">🎞 ${esc(r.name)}</span>
      <span class="chip ${isErr?"c-err":"c-done"}">${isErr?"异常":"已交付"}</span>
      ${r.verdict&&r.verdict!=="?"?`<span class="chip ${r.verdict==="good"?"c-done":"c-queued"}">质检 ${esc(r.verdict)}</span>`:""}
      <span class="ddate">${fmtDate(r.finished_at)} · 用时 ${fmtT(r.duration_s||0)} · ${(r.segments||[]).length} 段</span>
      ${!isErr?`<a class="dlbtn" href="/download?name=${encodeURIComponent(base+"/"+base+".mp4")}">${DL_SVG}下载整片</a>`:""}
      ${delBtnHtml()}</div>
    <div class="ddec">${esc(filmDoneLine(r))}</div>
    ${r.error?`<div class="derr">✗ ${esc(r.error)}</div>`:""}
    <div class="dfoot"><span class="dpath">保存于 ${esc(r.output||("H:\\h3cache\\films\\"+base+"\\"+base+".mp4"))}</span></div>
    <div class="fsegs">${segRowsHtml(base,r.segments)}</div>`;
  bindDelBtn(el,r.name);
  el.querySelector(".dcard-head").addEventListener("click",e=>{
    if(e.target.closest(".dlbtn")||e.target.closest(".delbtn"))return;
    el.classList.toggle("open");});
  return el;}

function renderDone(){
  const his=(DATA&&DATA.history)||[];
  const sig=JSON.stringify(his.map(r=>[r.name,r.state,r.finished_at,r.verdict]));
  if(sig===doneSig)return;doneSig=sig;
  doneList.innerHTML="";
  if(!his.length){
    doneList.innerHTML='<div class="empty">暂无已完成记录 · 成片交付后归档于此</div>';return;}
  for(const r of his)doneList.appendChild(r.film?buildFilmDoneCard(r):buildDoneCard(r));
}

function searchItems(q){
  const ql=q.toLowerCase(),qdig=q.replace(/\D/g,"");
  const items=[];
  const hit=(name,ts)=>name.toLowerCase().includes(ql)||(qdig&&ymd(ts).includes(qdig));
  for(const t of ((DATA&&DATA.tasks)||[]))
    if(hit(t.name,t.queued_at||t.started_at))items.push({kind:"task",t});
  for(const t of ((DATA&&DATA.films)||[]))
    if(hit(t.name+".film",t.queued_at||t.started_at))items.push({kind:"film",t});
  for(const r of ((DATA&&DATA.history)||[])){
    if(items.some(x=>x.t.name===r.name))continue;  // 同名优先取 tasks 实时态
    if(hit(r.name,r.finished_at))items.push({kind:"his",r});
  }
  return items;
}

function renderSearch(q){
  const items=searchItems(q);
  const sig=JSON.stringify(items.map(x=>x.kind==="task"
    ?[x.t.name,x.t.state,x.t.cur,x.t.pos,x.t.verdict,x.t.error]
    :x.kind==="film"?[x.t.name,x.t.state,x.t.phase,x.t.pct,x.t.error]
    :[x.r.name,x.r.state,x.r.finished_at,x.r.verdict]));
  if(sig===searchSig)return;searchSig=sig;
  const st=searchList.scrollTop;
  searchList.innerHTML="";
  if(!items.length){
    searchList.innerHTML=`<div class="empty">无匹配结果 · <b>${esc(qInput.value.trim())}</b></div>`;
    return;}
  for(const x of items)
    searchList.appendChild(x.kind==="task"?buildTaskCard(x.t)
      :x.kind==="film"?buildFilmCard(x.t)
      :(x.r.film?buildFilmDoneCard(x.r):buildDoneCard(x.r)));
  searchList.scrollTop=st;
}

function renderViews(){
  const q=(qInput.value||"").trim();
  const searching=q.length>0;
  board.style.display=(searching||view!=="run")?"none":"";
  doneList.style.display=(!searching&&view==="done")?"block":"none";
  searchList.style.display=searching?"block":"none";
  if(searching)renderSearch(q);
  else if(view==="done")renderDone();
}

function setView(v){view=v;
  segRun.classList.toggle("active",v==="run");
  segDone.classList.toggle("active",v==="done");
  renderViews();}
segRun.addEventListener("click",()=>setView("run"));
segDone.addEventListener("click",()=>setView("done"));
qInput.addEventListener("input",renderViews);

/* 配乐开关：off(本片不配乐) ↔ on(交给 JEV 决策；JEV 说不需要就不配) */
const BGM_LABEL={off:"配乐·关",on:"配乐·开"};
const BGM_NEXT={off:"on",on:"off"};
async function cycleBgm(name,cur){
  const rec=lanes.get(name);if(!rec)return;
  const btn=rec.bgmBtn;const next=BGM_NEXT[cur]||"off";
  btn.disabled=true;btn.classList.add("busy");
  try{
    const r=await fetch("/task/bgm",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name:name,mode:next})});
    const d=await r.json();
    if(d.ok)applyBgmMode(rec,d.bgm_manual||next);
  }catch(e){}
  finally{btn.classList.remove("busy")}
  btn.disabled=false;
}
function applyBgmMode(rec,mode){
  const btn=rec.bgmBtn;if(!btn)return;
  const m=BGM_LABEL[mode]?mode:"off";
  btn.textContent=BGM_LABEL[m];
  btn.className="bgmsw bgm "+m;
  btn.dataset.mode=m;
}

/* 音色开关：off(原音) ↔ on(Seed-VC 角色音色；硬闸不过自动回退原音) */
const VOICE_LABEL={off:"音色·关",on:"音色·开"};
const VOICE_NEXT={off:"on",on:"off"};
async function cycleVoice(name,cur){
  const rec=lanes.get(name);if(!rec)return;
  const btn=rec.voiceBtn;const next=VOICE_NEXT[cur]||"off";
  btn.disabled=true;btn.classList.add("busy");
  try{
    const r=await fetch("/task/voice",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name:name,mode:next})});
    const d=await r.json();
    if(d.ok)applyVoiceMode(rec,d.voice_manual||next);
  }catch(e){}
  finally{btn.classList.remove("busy")}
  btn.disabled=false;
}
function applyVoiceMode(rec,mode){
  const btn=rec.voiceBtn;if(!btn)return;
  const m=VOICE_LABEL[mode]?mode:"off";
  btn.textContent=VOICE_LABEL[m];
  btn.className="bgmsw voice "+m;
  btn.dataset.mode=m;
}

function bindRailDrag(rec){
  const w=rec.rail;let down=null;
  w.addEventListener("pointerdown",e=>{down={x:e.clientX,l:w.scrollLeft};
    rec.railHold=Date.now();
    try{w.setPointerCapture(e.pointerId)}catch(_){}
    w.classList.add("grabbing");});
  w.addEventListener("pointermove",e=>{if(down){w.scrollLeft=down.l+down.x-e.clientX;}});
  const up=()=>{down=null;w.classList.remove("grabbing");};
  w.addEventListener("pointerup",up);w.addEventListener("pointercancel",up);
  w.addEventListener("wheel",e=>{
    rec.railHold=Date.now();
    if(w.scrollWidth>w.clientWidth&&!e.shiftKey){w.scrollLeft+=e.deltaY;e.preventDefault();}
  },{passive:false});
}

function buildLane(t,d){
  const el=document.createElement("section");
  el.className="lane";
  el.innerHTML=`<div class="lane-head">
      <span class="fname" title="${esc(t.name)}">${esc(t.name)}</span>
      <span class="chip"></span>
      <button class="bgmsw bgm off" title="配乐开关：关=本片不配乐；开=交给 JEV 决策（JEV 说不需要就不配）">配乐·关</button>
      <button class="bgmsw voice off" title="音色开关：关=保留原音；开=Seed-VC 角色音色（硬闸不过自动回退原音）">音色·关</button>
      <a class="dlbtn" href="/download?name=${encodeURIComponent(t.name)}">${DL_SVG}下载成片</a>
      <span class="lmeta"><span class="pct"></span><span class="eta"></span></span></div>
    <div class="rail-wrap"><div class="rail"></div></div>
    <div class="curline"></div>
    <div class="logbox"><div class="log-lines"></div></div>
    <button class="pill"></button>`;
  board.appendChild(el);
  const rec={el,seq:0,lastCur:null,built:false,railHold:0,stick:true,pending:0,dispPct:-1,
             rail:el.querySelector(".rail-wrap"),
             log:el.querySelector(".log-lines"),box:el.querySelector(".logbox"),
             pill:el.querySelector(".pill"),
             chip:el.querySelector(".chip"),pct:el.querySelector(".pct"),
             eta:el.querySelector(".eta"),cur:el.querySelector(".curline"),nodes:{},
             bgmBtn:el.querySelector(".bgmsw.bgm"),
             voiceBtn:el.querySelector(".bgmsw.voice")};
  rec.railEl=rec.rail.firstElementChild;
  rec.bgmBtn.addEventListener("click",()=>cycleBgm(t.name,rec.bgmBtn.dataset.mode||"off"));
  rec.voiceBtn.addEventListener("click",()=>cycleVoice(t.name,rec.voiceBtn.dataset.mode||"off"));
  bindRailDrag(rec);
  // 粘性日志：在底部=跟随最新；上滚=翻阅历史，不被新输出顶走
  rec.box.addEventListener("scroll",()=>{
    const atB=rec.box.scrollTop+rec.box.clientHeight>=rec.box.scrollHeight-8;
    if(atB){rec.stick=true;rec.pending=0;rec.pill.className="pill";}
    else{rec.stick=false;}
  });
  rec.pill.addEventListener("click",()=>{
    rec.stick=true;rec.pending=0;rec.pill.className="pill";
    rec.box.scrollTop=rec.box.scrollHeight;
  });
  lanes.set(t.name,rec);
  return rec;
}

function buildRail(rec,d){
  rec.railEl.innerHTML="";
  (d.stages||[]).forEach((s,i)=>{
    let conn=null;
    if(i){conn=document.createElement("div");conn.className="conn";rec.railEl.appendChild(conn);}
    const n=document.createElement("div");
    n.className="stage s-pending";
    n.innerHTML=`<div class="nwrap"><div class="ring"></div><div class="node"><span class="mark">${i+1}</span></div></div>
      <div class="sname">${esc(s.label)}</div>
      <div class="ssub">${esc(s.desc)}</div>
      <div class="sbar"><div></div></div>
      <div class="smeta"></div>`;
    rec.railEl.appendChild(n);
    rec.nodes[s.key]={el:n,conn,mark:n.querySelector(".mark"),
                      bar:n.querySelector(".sbar div"),meta:n.querySelector(".smeta")};
  });
  rec.built=true;
}

function computePct(t,d,now){
  if(t.state==="done")return 100;
  if(t.state==="queued")return 0;
  let done=0,total=0;
  for(const s of (d.stages||[])){
    const w=Math.max(s.eta,5);total+=w;
    const st=(t.stages||{})[s.key]||{};
    if(st.state==="done"||st.state==="skip"||st.state==="fail")done+=w;
    else if(st.state==="active"&&st.start){
      done+=w*Math.min(.96,(now-st.start)/s.eta);
    }
  }
  return total?Math.round(done/total*100):0;
}

function updateLane(t,d,now){
  const rec=lanes.get(t.name)||buildLane(t,d);
  rec.t=t;
  if(!rec.built)buildRail(rec,d);
  const [ctext,ccls]=CHIP[t.state]||[t.state,"c-queued"];
  rec.chip.textContent=ctext;rec.chip.className="chip "+ccls;
  rec.el.classList.toggle("done",t.state==="done");
  rec.el.classList.toggle("error",t.state==="error");
  const mode=BGM_LABEL[t.bgm_manual]?t.bgm_manual:"off";
  if(rec.bgmBtn.dataset.mode!==mode){rec.bgmBtn.dataset.mode=mode;applyBgmMode(rec,mode);}
  const vmode=VOICE_LABEL[t.voice_manual]?t.voice_manual:"off";
  if(rec.voiceBtn.dataset.mode!==vmode){rec.voiceBtn.dataset.mode=vmode;applyVoiceMode(rec,vmode);}
  // 配乐/音色按钮全程可见：done/error 已交付，仅展示当时的决策（禁点）
  const delivered=t.state==="done"||t.state==="error";
  rec.bgmBtn.disabled=delivered;
  rec.voiceBtn.disabled=delivered;
  rec.voiceBtn.title=delivered?"已交付 · 此为当时的音色决策"
    :"音色本片把控：关(保留原音) ↔ 开(Seed-VC；硬闸不过自动回退原音)";
  rec.bgmBtn.title=delivered?"已交付 · 此为当时的配乐决策"
    :"配乐本片把控：自动(听 JEV) → 关(本片不用) → 开(本片必配)";
  // 百分比数字缓动
  const target=computePct(t,d,now);
  rec.dispPct=rec.dispPct<0?target:rec.dispPct+(target-rec.dispPct)*0.35;
  if(Math.abs(target-rec.dispPct)<0.6)rec.dispPct=target;
  rec.pct.textContent=Math.round(rec.dispPct)+"%";
  let etaTxt="";
  if(t.state==="running"){
    let rem=0;
    for(const s of (d.stages||[])){
      const st=(t.stages||{})[s.key]||{};
      if(st.state==="pending")rem+=s.eta;
      else if(st.state==="active"&&st.start)rem+=Math.max(0,s.eta-(now-st.start));
    }
    etaTxt="剩余约 "+fmtT(rem);
  }else if(t.state==="queued"){etaTxt=t.pos===1?"即将开工":("队列第 "+(t.pos||"?")+" 位");}
  else if(t.state==="done"&&t.started_at&&t.ended_at){
    etaTxt="用时 "+fmtT(t.ended_at-t.started_at);}
  rec.eta.textContent=etaTxt;

  let prevDone=true;
  for(const s of (d.stages||[])){
    const n=rec.nodes[s.key];if(!n)continue;
    const st=(t.stages||{})[s.key]||{state:"pending"};
    n.el.className="stage s-"+st.state;
    if(n.conn){
      n.conn.className="conn"+((st.state==="done"||st.state==="skip")?" on"
        :(st.state==="active"&&prevDone)?" flow":"");
    }
    prevDone=(st.state==="done"||st.state==="skip");
    if(st.state==="pending"){
      n.mark.textContent=n.mark.textContent||String((d.stages||[]).indexOf(s)+1);
      n.bar.style.width="0%";n.meta.textContent="";
    }else if(st.state==="active"){
      n.mark.textContent="▶";
      const el=now-(st.start||now);
      n.bar.style.width=(s.eta>0?Math.min(96,el/s.eta*100):0).toFixed(1)+"%";
      const r=s.eta-el;
      n.meta.textContent=r>0?("剩余 "+fmtT(r)):"收尾中…";
    }else if(st.state==="done"){
      n.mark.textContent="✓";n.bar.style.width="100%";
      n.meta.textContent=(st.start&&st.end)?fmtT(st.end-st.start):"";
    }else if(st.state==="skip"){
      n.mark.textContent="→";n.bar.style.width="0%";n.meta.textContent="已跳过";
    }else if(st.state==="fail"){
      n.mark.textContent="✗";n.bar.style.width="0%";n.meta.textContent="失败";
    }
  }

  if(t.state==="queued"){
    rec.cur.innerHTML=t.pos===1?"排队待处理 · 即将开工":"排队待处理 · 队列第 "+(t.pos||"?")+" 位";
  }else if(t.cur&&t.stages&&t.stages[t.cur]){
    const st=t.stages[t.cur];
    rec.cur.innerHTML=`<b>${esc(stageLabel(t.cur,d))}</b> · ${esc(st.detail||"")}`;
  }else if(t.state==="done"){
    rec.cur.innerHTML=`成片 <b>H:\\h3cache\\${esc(t.name)}</b>${t.verdict?" · 质检 "+esc(t.verdict):""}`;
  }else if(t.state==="error"){
    rec.cur.innerHTML=`<span style="color:var(--danger)">✗ ${esc(t.error||"处理异常")}</span>`;
  }

  if(t.cur&&t.cur!==rec.lastCur){
    rec.lastCur=t.cur;
    const n=rec.nodes[t.cur];
    if(n&&Date.now()-rec.railHold>8000){
      rec.rail.scrollTo({left:n.el.offsetLeft-rec.rail.clientWidth/2+n.el.offsetWidth/2,
                         behavior:"smooth"});
    }
  }

  const evs=(d.events||[]).filter(e=>e.task===t.name&&e.seq>rec.seq);
  if(evs.length){
    for(const e of evs){
      rec.seq=e.seq;
      const line=document.createElement("div");line.className="ll";
      line.innerHTML=`<span class="lt">${fmtClock(e.ts)}</span>`+
        `<span class="ls">${esc(stageLabel(e.stage,d))}</span>`+
        `<span class="lx">${esc(e.text)}</span>`;
      rec.log.appendChild(line);
      if(!rec.stick)rec.pending++;
    }
    while(rec.log.children.length>80)rec.log.firstChild.remove();
    if(rec.stick){rec.box.scrollTop=rec.box.scrollHeight;}
    else{
      const p=rec.pill;
      p.textContent=rec.pending+" 条新输出 · 回到最新";
      p.className="pill show";
    }
  }
}

// chip 状态映射（updateLane 内直接使用 CHIP）
/* —— 长片（film）运行视图：可折叠长片卡（片名+总进度+整片下载；展开见 seg_XX 子项行） —— */
const flanes=new Map();
function buildFilmLane(t){
  const el=document.createElement("section");
  el.className="flane";
  el.innerHTML=`<div class="lane-head">
      <span class="fcaret">▶</span>
      <span class="fname" title="${esc(t.name)}.film">🎞 ${esc(t.name)}.film</span>
      <span class="chip"></span>
      <a class="dlbtn" href="/download?name=${encodeURIComponent(t.name+"/"+t.name+".mp4")}">${DL_SVG}下载整片</a>
      <span class="lmeta"><span class="pct"></span><span class="feta"></span></span></div>
    <div class="curline"></div>
    <div class="fnote"></div>
    <div class="fsegs"></div>`;
  board.appendChild(el);
  const rec={el,chip:el.querySelector(".chip"),pct:el.querySelector(".pct"),
             feta:el.querySelector(".feta"),cur:el.querySelector(".curline"),
             note:el.querySelector(".fnote"),
             segsEl:el.querySelector(".fsegs"),rows:[]};
  el.querySelector(".lane-head").addEventListener("click",e=>{
    if(e.target.closest(".dlbtn"))return;
    el.classList.toggle("open");});  // 折叠态存 DOM class，重渲染不丢
  flanes.set(t.name,rec);
  return rec;}

function updateFilmLane(t){
  const rec=flanes.get(t.name)||buildFilmLane(t);
  const [ct,ccls]=FCHIP[t.state]||[t.state,"c-queued"];
  rec.chip.textContent=ct;rec.chip.className="chip "+ccls;
  rec.el.classList.toggle("done",t.state==="done");
  rec.el.classList.toggle("error",t.state==="error");
  rec.pct.textContent=(t.pct||0)+"%";
  let eta="";
  if(t.state==="queued")eta=t.pos===1?"即将开工":("队列第 "+(t.pos||"?")+" 位");
  else if(t.state==="running"&&t.eta){
    // 长片 ETA（服务端每次 /status 现算）：本段预计 = 当前段剩余，总剩余 = 剩余段+收尾阶段
    const sr=t.eta.seg_remaining_s,fm=t.eta.film_remaining_s;
    eta=(sr!=null?"本段预计 "+fmtT(sr):"段间切换")+" / 总剩余 "+fmtT(fm);
  }
  else if(t.state==="done"&&t.started_at&&t.ended_at)eta="用时 "+fmtT(t.ended_at-t.started_at);
  rec.feta.textContent=eta;
  if(t.state==="error")
    rec.cur.innerHTML=`<span style="color:var(--danger)">✗ ${esc(t.error||"处理异常")}</span>`;
  else rec.cur.innerHTML=`<b>${esc(t.phase_label||"调度")}</b> · ${esc(t.phase_text||"")}`;
  if(t.note){rec.note.textContent=t.note;rec.note.style.display="";}
  else rec.note.style.display="none";
  const segs=t.segments||[];
  if(rec.rows.length!==segs.length){  // 段数量变化才重建行
    rec.segsEl.innerHTML="";rec.rows=[];
    for(const s of segs){
      const row=document.createElement("div");
      row.innerHTML=`<span class="sdot"></span><span class="sname">${esc(segName(s))}</span>`+
        `<span class="sret"></span><span class="sv"></span>`+
        `<a class="sdl" href="/download?name=${encodeURIComponent(t.name+"/"+segName(s)+".mp4")}">下载</a>`;
      rec.segsEl.appendChild(row);
      rec.rows.push({row,ret:row.querySelector(".sret"),sv:row.querySelector(".sv")});}
  }
  segs.forEach((s,i)=>{
    const r=rec.rows[i];if(!r)return;
    r.row.className="fseg "+(SEG_DOT[s.state]||"s-pending");
    r.ret.textContent=s.retries?("重试"+s.retries):"";
    r.sv.innerHTML=(s.verdict?`<b class="${s.verdict==="good"?"v-good":"v-bad"}">质检 ${esc(s.verdict)}</b> `:"")
      +esc(s.note||SEG_LABEL[s.state]||s.state||"");});
}

function render(d){
  DATA=d;const now=d.now||Date.now()/1000;
  const ts=d.tasks||[];
  const fs=d.films||[];
  const hs=d.history||[];  // 已交付/异常与已完成列表同源：读持久化 history（重启不清零）
  const run=ts.filter(t=>t.state==="running").length
           +fs.filter(t=>t.state==="running").length;
  const que=ts.filter(t=>t.state==="queued").length
           +fs.filter(t=>t.state==="queued").length;
  const don=hs.filter(r=>r.state==="done").length;
  const err=hs.filter(r=>r.state==="error").length;
  document.getElementById("hstat").innerHTML=
    `制作中 <b>${run}</b> · 排队 <b>${que}</b> · 已交付 <b>${don}</b>`+
    (err?` · 异常 <b style="color:var(--danger)">${err}</b>`:"")+
    `　｜　${esc((d.overall||{}).text||"")}`;
  emptyEl.style.display=(ts.length||fs.length)?"none":"";
  // 有序挂载：仅当顺序变化时移动节点，且保留日志滚动位置（修复跳顶）
  const current=Array.from(board.querySelectorAll(".lane")).map(c=>c.dataset.name);
  ts.forEach((t,i)=>{
    const rec=lanes.get(t.name);
    if(rec&&current[i]!==t.name){
      const st=rec.box.scrollTop;
      board.appendChild(rec.el);
      rec.box.scrollTop=st;
    }
  });
  for(const t of ts){
    updateLane(t,d,now);           // updateLane 内部对缺失线路兜底 buildLane
    const rec=lanes.get(t.name);
    if(rec)rec.el.dataset.name=t.name;
  }
  for(const t of fs)updateFilmLane(t);  // 长片卡追加在视频线路之后（flane 独立 class，不扰 lane 排序）
  renderViews();
}

async function poll(){
  try{
    const r=await fetch("/status");
    render(await r.json());
  }catch(e){
    document.getElementById("hstat").textContent="导演台离线 · 重试中…";
  }
}
poll();setInterval(poll,1500);
setInterval(()=>{if(DATA)for(const t of (DATA.tasks||[])){
  const rec=lanes.get(t.name);if(rec)updateLane(t,DATA,Date.now()/1000);}},400);
</script></body></html>"""


def _mirror_download(fp, rel_parts):
    """把 /download 交付文件同步镜像到 H:\\h3cache\\downloads（同大小跳过重拷）。
    返回目标路径；失败只记日志，绝不影响浏览器下载本身。"""
    try:
        dst = os.path.join(DOWNLOADS, *rel_parts)
        os.makedirs(os.path.dirname(dst) or DOWNLOADS, exist_ok=True)
        if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(fp):
            return dst
        tmp = dst + ".part"
        shutil.copy2(fp, tmp)
        os.replace(tmp, dst)
        return dst
    except OSError as e:
        log(f"[download] 镜像到 {DOWNLOADS} 失败: {e}")
        return None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlparse(self.path).path  # 剥掉 query 再匹配（支持 /?embed=1）
        if route == "/status":
            body = json.dumps(_flush(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif route == "/download":
            qs = parse_qs(urlparse(self.path).query)
            raw = (qs.get("name") or [""])[0]
            if "/" in raw or "\\" in raw:
                # 长片产物：name=<片名>/<片名>.mp4 或 <片名>/seg_XX.mp4，在 films 目录解析；
                # 逐段过 basename 且拒绝盘符/上跳，防穿越语义与单片分支一致
                parts = [os.path.basename(p.strip()) for p in
                         raw.replace("\\", "/").split("/")]
                if not parts or any(p in ("", ".", "..") or ":" in p
                                    for p in parts):
                    self.send_response(404)
                    self.end_headers()
                    return
                fp = os.path.join(FILMS, *parts)
                rel_parts = parts
            else:
                fp = os.path.join(OUT, os.path.basename(raw))  # 仅取文件名，防穿越
                rel_parts = [os.path.basename(fp)]
            name = os.path.basename(fp)
            if not name or not os.path.isfile(fp):
                self.send_response(404)
                self.end_headers()
                return
            _mirror_download(fp, rel_parts)  # 下载落 H:\h3cache\downloads（失败不影响传输）
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(name)}")
            self.send_header("Content-Length", str(os.path.getsize(fp)))
            self.end_headers()
            with open(fp, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
        elif route == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")  # 防抽屉 iframe 吃到旧页面
            self.end_headers()
            self.wfile.write(HTML.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def _json_reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        route = urlparse(self.path).path  # 剥掉 query 再匹配
        if route == "/task/delete":
            self._handle_delete()
            return
        if route not in ("/task/bgm", "/task/voice"):
            self.send_response(404)
            self.end_headers()
            return
        is_voice = route == "/task/voice"
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
            name = str(d.get("name") or "")
            mode = str(d.get("mode") or "")
            modes = ("on", "off") if is_voice else ("auto", "on", "off")
            if mode not in modes:
                raise ValueError("bad mode")
        except Exception:
            self._json_reply(400, {"ok": False,
                                   "error": ("bad request: {\"name\": str, \"mode\": \"on|off\"} required"
                                             if is_voice else
                                             "bad request: {\"name\": str, \"mode\": \"auto|on|off\"} required")})
            return
        with _lock:
            t = _tasks.get(name)
            if not t:
                self._json_reply(404, {"ok": False, "error": "task not found: " + name})
                return
            if t["state"] in ("done", "error"):
                self._json_reply(409, {"ok": False, "error": "task already finished"})
                return
            t["voice_manual" if is_voice else "bgm_manual"] = mode
        if is_voice:
            ev(name, None, "音色手动把控 → " + {"on": "开启", "off": "关闭"}[mode])
            log(f"[{name}] 音色手动把控 → {mode}")
            self._json_reply(200, {"ok": True, "name": name, "voice_manual": mode})
            return
        ev(name, None, "配乐手动把控 → " + {"auto": "自动（听 JEV）", "on": "强制开启",
                                            "off": "强制关闭"}[mode])
        log(f"[{name}] 配乐手动把控 → {mode}")
        self._json_reply(200, {"ok": True, "name": name, "bgm_manual": mode})

    def _handle_delete(self):
        """删除历史记录 + 其磁盘产物（单片 OUT\\<name> / 长片 FILMS\\<base>）。
        成功才移除列表条目；任何一步失败都返回错误并保留记录。"""
        global _history_records
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
            name = str(d.get("name") or "")
        except Exception:
            self._json_reply(400, {"ok": False,
                                   "error": "bad request: {\"name\": str} required"})
            return
        if not _artifact_target(name, False):
            self._json_reply(400, {"ok": False, "error": "invalid name: " + name})
            return
        with _lock:
            rec, idx = None, None
            for i, r in enumerate(_history_records):
                if isinstance(r, dict) and r.get("name") == name:
                    rec, idx = r, i
                    break
            if rec is None:
                self._json_reply(404, {"ok": False,
                                       "error": "history record not found: " + name})
                return
            is_film = bool(rec.get("film")) or name.endswith(".film")
            path, root, base = _artifact_target(name, is_film)
            if is_film:
                f = _films.get(base)
                active = bool(f and f.get("state") in ("queued", "running"))
            else:
                t = _tasks.get(name)
                active = bool(t and t.get("state") in ("queued", "running"))
            if active:
                self._json_reply(409, {"ok": False,
                                       "error": "task is active: " + name})
                return
        real = os.path.realpath(path)
        protected = {os.path.realpath(p) for p in
                     (OUT, FILMS, RAW, WORK, CACHE, DOWNLOADS, FAIL, ORIG, BGM_OUT)}
        if not _within_root(real, os.path.realpath(root)) or real in protected:
            self._json_reply(400, {"ok": False,
                                   "error": "target outside artifact root: " + name})
            return
        # 先删磁盘：失败则历史记录原样保留
        try:
            if os.path.isdir(real):
                shutil.rmtree(real)
            elif os.path.lexists(real):
                os.remove(real)
        except OSError as e:
            self._json_reply(500, {"ok": False,
                                   "error": "delete failed: " + str(e)[:200]})
            return
        # 再持久化移除：落盘失败则回滚内存列表，列表条目保持不变
        with _history_lock:
            _history_records = [r for r in _history_records if r is not rec]
            save_err = None
            try:
                _write_history_locked()
            except Exception as e:
                save_err = str(e)[:120]
                _history_records.insert(min(idx, len(_history_records)), rec)
        if save_err:
            self._json_reply(500, {"ok": False,
                                   "error": "history save failed: " + save_err})
            return
        # 同步清掉同名终态内存任务，避免运行视图残留幽灵卡
        with _lock:
            if base is not None:
                f = _films.get(base)
                if f and f.get("state") in ("done", "error"):
                    _films.pop(base, None)
            else:
                t = _tasks.get(name)
                if t and t.get("state") in ("done", "error"):
                    _tasks.pop(name, None)
        log(f"[{name}] 历史记录与磁盘产物已删除 · {real}")
        self._json_reply(200, {"ok": True, "name": name, "removed": real})

    def log_message(self, *a):
        pass


class SingleInstanceHTTPServer(ThreadingHTTPServer):
    """多线程：下载成片等慢请求不阻塞 /status 轮询。
    Windows 的 SO_REUSEADDR 允许同端口双绑定（实测 SECOND_BIND_OK），
    「端口即单实例锁」必须改用 SO_EXCLUSIVEADDRUSE 才能真正拒绝第二实例"""
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        import socket
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        ThreadingHTTPServer.server_bind(self)


def urllib_get(url, timeout=5):
    import urllib.request
    return urllib.request.urlopen(url, timeout=timeout)


def comfy_busy():
    """ComfyUI 运行中或排队中均视为忙；查询失败返回 None"""
    try:
        with urllib_get(COMFY + "/queue") as r:
            d = json.load(r)
        return len(d.get("queue_running", [])) + len(d.get("queue_pending", [])) > 0
    except Exception:
        return None


def probe_duration(video):
    import re as _re
    r = subprocess.run([FFMPEG, "-i", video], capture_output=True,
                       text=True, creationflags=NO_WINDOW)
    m = _re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr or "")
    if not m:
        return 0.0
    hh, mm, ss = m.groups()
    return int(hh) * 3600 + int(mm) * 60 + float(ss)


def probe_media(video):
    """返回 (宽, 高)，解析 ffmpeg 流信息"""
    import re as _re
    r = subprocess.run([FFMPEG, "-i", video], capture_output=True,
                       text=True, creationflags=NO_WINDOW)
    m = _re.search(r"Stream #.*Video:.*?, (\d{2,5})x(\d{2,5})", r.stderr or "")
    return (int(m.group(1)), int(m.group(2))) if m else (1280, 720)


def load_dialogue(base=None):
    """全局台词配置 dialogue.json；视频同名 .dialogue.json 存在时按视频覆盖"""
    if base:
        ov = os.path.join(RAW, os.path.splitext(base)[0] + ".dialogue.json")
        if os.path.exists(ov):
            with open(ov, encoding="utf-8") as f:
                return json.load(f)
    with open(DIALOGUE, encoding="utf-8") as f:
        return json.load(f)


def load_audio_16k(video):
    import numpy as np
    wav = video + ".16k.wav"
    subprocess.run([FFMPEG, "-y", "-i", video, "-vn", "-ar", "16000",
                    "-ac", "1", "-f", "wav", wav],
                   capture_output=True, creationflags=NO_WINDOW)
    if not os.path.exists(wav):
        raise RuntimeError("ffmpeg 抽取音频失败")
    with wave.open(wav, "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()),
                             dtype=np.int16).astype(np.float32) / 32768.0
    os.remove(wav)
    return data


def wrap_text(text, font_file, size, max_w=1100):
    from PIL import ImageFont
    try:
        f = ImageFont.truetype(font_file, size)
    except Exception:
        f = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", size)
    out, cur = [], ""
    for ch in text:
        if cur and f.getlength(cur + ch) > max_w:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def build_ass(windows, lines, out_ass, font_family="Microsoft YaHei",
              font_file=None, video_w=1280, video_h=720, h3_style=False):
    """PlayRes 直接取视频实际尺寸（坐标=像素，杜绝横竖屏缩放歧义）；
    字号/边距按 720p 基准随高度缩放，换行宽度=画宽减两边距。
    h3_style=True 时按 H3 自带字幕实测样式（Noto Sans SC、常规字重、小字号、底部偏上）烧录。"""
    scale = video_h / 720.0
    if h3_style:
        # H3 自带字幕实测（480x864）：字高约 23px、文字底部距画面底约 174px、单行、白字黑边
        fs_bottom = max(18, round(25 * scale))
        fs_top = max(16, round(20 * scale))
        margin = max(8, round(8 * scale))
        margin_v = max(24, round(145 * scale))
        bold, outline, shadow, spacing = 0, 2, 0, 0.5
        font_family = "Noto Sans SC"
        font_file = r"C:\Windows\Fonts\Noto Sans SC.ttf"
    else:
        fs_bottom = max(28, round(62 * scale))
        fs_top = max(22, round(46 * scale))
        margin = max(24, round(60 * scale))
        margin_v = max(18, round(45 * scale))
        bold, outline, shadow, spacing = -1, 3, 1, 1
    max_w = video_w - 2 * margin
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {video_w}\nPlayResY: {video_h}\n"
            "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
            "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,{font_family},{fs_bottom},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
            f"{bold},0,0,0,100,100,{spacing},0,1,{outline},{shadow},2,{margin},{margin},{margin_v},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")

    def fmt(t):
        return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"

    out = [head]
    for (s, e), l in zip(windows, lines):
        top = (l.get("top") or "").strip()
        bottom = (l.get("bottom") or "").strip()
        parts = []
        if top:
            for seg in wrap_text(top, r"C:\Windows\Fonts\msyhbd.ttc", fs_top, max_w=max_w):
                parts.append(f"{{\\fs{fs_top}\\c&H00E8E8E8&}}{seg}")
        if bottom:
            for seg in wrap_text(bottom, font_file or r"C:\Windows\Fonts\msyhbd.ttc",
                                 fs_bottom, max_w=max_w):
                parts.append(f"{{\\fs{fs_bottom}}}{seg}")
        text = "\\N".join(parts)
        out.append(f"Dialogue: 0,{fmt(s)},{fmt(e)},Default,,0,0,0,,{text}\n")
    with open(out_ass, "w", encoding="utf-8-sig") as f:
        f.write("".join(out))


def interpolate_frames(video, target_fps=48):
    """minterpolate 运动补偿插帧（24→48fps）。返回新视频路径。"""
    out = os.path.splitext(video)[0] + "_48fps.mp4"
    fc = (f"minterpolate=fps={target_fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
    r = subprocess.run(
        [FFMPEG, "-y", "-i", video, "-vf", fc,
         "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-profile:v", "main", "-level", "4.0",
         "-c:a", "copy", out],
        capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("插帧失败: " + (r.stderr or "")[-200:])
    return out


def _norm_sub(s):
    """字幕对齐用：去掉标点/空格，只留中英文数字"""
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "")


def subtitle_video(video, progress, decision, dlg, style=None):
    """语音对齐 + 双语字幕烧录。progress(detail) 汇报子步骤；decision 为 JEV 决策。
    style="h3" 时使用 H3 自带字幕的实测样式（长片统一风格）。"""
    lang = dlg.get("lang", "zh")
    lines = dlg["lines"]
    fi = FONTS.get(decision.get("font", "雅黑"), FONTS["雅黑"])

    progress("音频抽取 · 16 kHz 单声道")
    audio = load_audio_16k(video)
    dur = probe_duration(video) or 5.0

    progress(f"Whisper 语音对齐 · {lang}")
    from faster_whisper import WhisperModel
    wm = r"E:\local-models\huggingface\hub\models--Systran--faster-whisper-small\snapshots\536b0662742c02347bc0e980a01041f333bce120"
    model = WhisperModel(wm if os.path.isdir(wm) else "small",
                         device="cpu", compute_type="int8")
    segments, _ = model.transcribe(audio, language=lang, vad_filter=True)
    raw_segs = list(segments)
    segs = [(s.start, s.end) for s in raw_segs]
    seg_texts = [s.text for s in raw_segs]

    if not segs:
        progress("未检出人声 · 均匀时间窗对齐")
        t, span = 0.5, dur / max(len(lines), 1)
        segs = [(t + i * span, t + (i + 1) * span) for i in range(len(lines))]
    cover = sum(e - s for s, e in segs) / max(dur, 0.1)
    if len(segs) >= len(lines):
        if cover < 0.3:
            # 人声覆盖过低（环境音零星误检）：回退均匀窗，避免大段空窗
            progress(f"人声覆盖率 {cover:.0%} 低 · 均匀时间窗对齐")
            t, span = 0.5, dur / max(len(lines), 1)
            windows = [(t + i * span, t + (i + 1) * span) for i in range(len(lines))]
        elif len(segs) == len(lines):
            windows = segs
        else:
            # 检出块多于台词：把相邻人声块按文本相似度分组成每句台词（多余块跳过）
            progress("人声块多于台词 · 文本匹配分组")
            norm_texts = [_norm_sub(t) for t in seg_texts]
            wants = [_norm_sub((l.get("top", "") or "") + (l.get("bottom", "") or ""))
                     for l in lines]
            n, m = len(wants), len(segs)
            NEG = -1e9
            dp = [[NEG] * (m + 1) for _ in range(n + 1)]
            back = [[None] * (m + 1) for _ in range(n + 1)]
            for j in range(m + 1):
                dp[0][j] = 0.0
            for i in range(1, n + 1):
                for j in range(1, m + 1):
                    for s in range(i - 1, j):
                        if dp[i - 1][s] <= NEG / 2:
                            continue
                        grp = "".join(norm_texts[s:j])
                        r = difflib.SequenceMatcher(
                            None, wants[i - 1], grp, autojunk=False).ratio()
                        val = dp[i - 1][s] + r
                        if val > dp[i][j]:
                            dp[i][j] = val
                            back[i][j] = s
            best_j = max(range(n, m + 1), key=lambda j: dp[n][j])
            groups, j = [], best_j
            for i in range(n, 0, -1):
                s = back[i][j]
                groups.append((s, j))
                j = s
            groups.reverse()
            windows = [(segs[s][0], segs[e - 1][1]) for s, e in groups]
    else:
        total = sum(max(len(l.get("top", "")) + len(l.get("bottom", "")), 1) for l in lines)
        # span 下限 70% 段长：防止全部台词挤进头部、段尾长死区
        span = min(max(segs[-1][1] - segs[0][0], dur * 0.7),
                   max(dur - segs[0][0], 0.5))
        windows, t = [], segs[0][0]
        for l in lines:
            d = span * max(len(l.get("top", "")) + len(l.get("bottom", "")), 1) / total
            windows.append((t, min(t + d, dur)))
            t += d

    progress(f"ASS 渲染 · {fi['family']}")
    vw, vh = probe_media(video)
    ass = os.path.join(os.path.dirname(video), "_sub_temp.ass")
    build_ass(windows, lines, ass, fi["family"], fi["file"],
              video_w=vw, video_h=vh, h3_style=(style == "h3"))

    progress("libx264 字幕烧录")
    out = os.path.splitext(video)[0] + "_sub.mp4"
    r = subprocess.run(
        [FFMPEG, "-y", "-i", os.path.basename(video),
         "-vf", f"ass={os.path.basename(ass)}",
         # 字幕烧录不得增删帧：VFR/CFR 源都按原始时间戳透传
         "-fps_mode", "passthrough",
         "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-profile:v", "main", "-level", "4.0",
         "-c:a", "copy", os.path.basename(out)],
        cwd=os.path.dirname(video), capture_output=True, text=True,
        creationflags=NO_WINDOW)
    try:
        os.remove(ass)
    except OSError:
        pass
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("字幕烧录失败: " + (r.stderr or "")[-200:])
    return out


def find_bgm():
    cands = []
    for d in BGM_DIRS:
        for ext in ("*.mp3", "*.flac", "*.wav", "*.m4a"):
            cands += glob.glob(os.path.join(d, ext))
    return max(cands, key=os.path.getmtime) if cands else None


def mix_bgm(video, bgm, volume=0.15, duck=True):
    """BGM 混入：BGM 短于成片时循环铺满、长于成片时裁剪到成片长度；
    头 1.5s 渐入、尾 2.5s 渐出（结尾不硬断）；duck=True 时人声作侧链压缩 BGM（自动闪避）。
    混音总线再叠 0.8s/2.5s 首尾淡化：BGM 淡出被人声掩盖时，成片首尾仍明确低于中段。"""
    out = os.path.splitext(video)[0] + "_bgm.mp4"
    vdur = probe_duration(video)
    if vdur <= 0:
        raise RuntimeError("混音失败: 无法读取成片时长")
    fade_out_start = max(vdur - 2.5, 0)
    # 先 atrim 到成片长度再 setpts：循环输入的时间戳归零，尾部淡出锚点才准确
    fade = (f"volume={volume},atrim=0:{vdur:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d=1.5,"
            f"afade=t=out:st={fade_out_start:.2f}:d=2.5[bgm];")
    if duck:
        # sidechaincompress 双输入：主输入=BGM，侧链=人声 → 对白时 BGM 自动压低
        fc = (f"[1:a]{fade}"
              f"[0:a]asplit=2[vo][sc];"
              f"[bgm][sc]sidechaincompress=threshold=0.03:ratio=6:attack=80:"
              f"release=500[duck];"
              f"[vo][duck]amix=inputs=2:duration=first:dropout_transition=0[mix];"
              f"[mix]afade=t=in:st=0:d=0.8,"
              f"afade=t=out:st={max(vdur - 2.5, 0):.3f}:d=2.5[a]")
    else:
        fc = (f"[1:a]{fade}"
              f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[mix];"
              f"[mix]afade=t=in:st=0:d=0.8,"
              f"afade=t=out:st={max(vdur - 2.5, 0):.3f}:d=2.5[a]")
    r = subprocess.run(
        [FFMPEG, "-y", "-i", video, "-stream_loop", "-1", "-i", bgm,
         "-filter_complex", fc,
         "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-ar", "48000", "-ac", "2", out],
        capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("混音失败: " + (r.stderr or "")[-150:])
    return out


def ensure_engine(timeout=180):
    """确认音乐引擎(8001)在线；不在线自动拉起（GPU、低优先级）"""
    def alive():
        try:
            with urllib_get("http://127.0.0.1:8001/health", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False
    if alive():
        return True
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command",
             "Start-Process -FilePath 'H:/MusicAI/scripts/start_engine_gpu.bat' -WindowStyle Hidden"],
            creationflags=NO_WINDOW)
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(5)
        if alive():
            return True
    return False


def gen_bgm_track(mood, bgm_type, duration_s):
    """唤起 ACE-Step 出曲：instrumental 纯音乐（--no-vocal）/ vocal 含人声演唱；返回新 mp3 路径"""
    caption = MOOD_CAPTIONS.get(mood, "氛围音乐")
    if bgm_type == "vocal":
        caption += "，含清晰人声演唱，情感充沛"
    os.makedirs(BGM_OUT, exist_ok=True)
    env = {**os.environ,
           "MUSICAI_ROOT": r"H:\MusicAI",
           "ACESTEP_PROJECT_ROOT": r"H:\MusicAI\apps\ACE-Step-1.5",
           "ACESTEP_CHECKPOINTS_DIR": r"H:\MusicAI\apps\ACE-Step-1.5\checkpoints",
           "ACESTEP_DOWNLOAD_SOURCE": "modelscope",
           "HF_HOME": r"H:\MusicAI\cache\hf",
           "MODELSCOPE_CACHE": r"H:\MusicAI\cache\ms",
           "TMP": r"H:\MusicAI\cache\tmp",
           "TEMP": r"H:\MusicAI\cache\tmp"}
    before = set(glob.glob(os.path.join(BGM_OUT, "*.mp3")))
    cmd = [ACE_PY, GEN_SONG]
    if bgm_type != "vocal":
        cmd.append("--no-vocal")
    cmd += ["--sample", caption,
            "--dur", str(max(12, int(duration_s) + 2)), "--steps", "8", "--fmt", "mp3",
            "-o", BGM_OUT]
    r = subprocess.run(
        cmd,
        env=env, cwd=r"H:\MusicAI", capture_output=True, text=True,
        timeout=2700, creationflags=NO_WINDOW)
    new = set(glob.glob(os.path.join(BGM_OUT, "*.mp3"))) - before
    if not new:
        log(f"[BGM] ACE-Step 无产出（rc={r.returncode}）：" + (r.stderr or "")[-160:])
    return max(new, key=os.path.getmtime) if new else None


def process_video(base):
    """单条线路全流程：decide → interp → subtitle → qc → bgm → deliver"""
    src = os.path.join(RAW, base)
    t0 = time.time()
    with _lock:
        t = _tasks.get(base)
        if t:
            t["state"] = "running"
            t["started_at"] = t0
            t["pos"] = None
            for other in _tasks.values():
                if other["state"] == "queued" and other["pos"] and other["pos"] > 1:
                    other["pos"] -= 1
    ev(base, None, "开工 · 工作副本就位")
    log(f"[{base}] 开始处理")

    work = os.path.join(WORK, base)
    jpath = None
    verdict = "?"
    dec = {}
    interp_on = False
    bilingual = False
    bgm_type = "instrumental"
    bgm_track_name = None
    bgm_manual = "off"
    voice_manual = "off"
    voice_info = None
    try:
        os.makedirs(WORK, exist_ok=True)
        shutil.copy2(src, work)

        # ── 1 语义决策：一次 JEV 推理产出 补帧/字体/配乐/情绪 全部策略 ──
        stage_begin(base, "decide", "JEV-4B 策略推理 · 补帧/字体/配乐")
        dlg = load_dialogue(base)
        dur = int(probe_duration(work)) or 5
        sj, dpath = work + ".state.json", work + ".decision.json"
        with open(sj, "w", encoding="utf-8") as f:
            json.dump({"style": dlg.get("style", "通用短视频"), "duration": dur,
                       "has_dialogue": bool(dlg.get("lines"))}, f)
        r = subprocess.run([JEV_PY, DECIDE_SCRIPT, sj, dpath],
                           capture_output=True, text=True, timeout=300,
                           creationflags=NO_WINDOW)
        dec = {}
        if os.path.exists(dpath):
            with open(dpath, encoding="utf-8") as f:
                dec = json.load(f)
        else:
            raise RuntimeError("JEV 决策未产出: " + (r.stderr or "")[-100:])
        with _lock:
            t = _tasks.get(base)
            if t:
                t["reasons"] = dec.get("reasons", {})
        bilingual = str(dec.get("bilingual", "no")).lower() == "yes"
        interp_on = dec.get("interpolate") == "yes"
        bgm_type = str(dec.get("bgm_type", "instrumental")).lower()
        if dec.get("bgm") == "none":
            bgm_txt = "无需配乐"
        else:
            bgm_txt = (MOOD_SHORT.get(dec.get("mood", ""), dec.get("mood", ""))
                       + "·" + ("有声" if bgm_type == "vocal" else "轻音乐"))
        if not bilingual:
            # 关闭双语 → 单语字幕：仅清空上行译文（dlg 为本次新加载的副本，直接改，不回写文件）
            for l in dlg.get("lines", []):
                l["top"] = ""
        stage_end(base, "decide",
                  detail=(f"补帧 {'开启' if interp_on else '关闭'} · "
                          f"字体 {dec.get('font', '雅黑')} · 配乐 {bgm_txt}"
                          f" · 双语 {'开' if bilingual else '关'}")
                  + ("（降级默认）" if dec.get("degraded") else ""))
        for p in (sj, dpath):
            try:
                os.remove(p)
            except OSError:
                pass

        # ── 2 帧率重构 ──
        if interp_on:
            stage_begin(base, "interp", "minterpolate 运动补偿 · 24→48fps")
            out = interpolate_frames(work, target_fps=48)
            os.replace(out, work)
            stage_end(base, "interp", detail="48fps 就绪")
        else:
            stage_end(base, "interp", "skip", detail="JEV 判定静态画面 · 跳过")

        # ── 3 质量评估：JEV 抽帧评分 + 自带字幕检测（音色/字幕之前完成）──
        stage_begin(base, "qc", "JEV-4B 关键帧采样评分")
        # 质检依赖 JEV 环境（NeoHorse-Jev-4B），必须用 JEV_PY 而非流水线自身解释器
        jr = subprocess.run([JEV_PY, JUDGE_SCRIPT, "--video", work],
                            capture_output=True, text=True, timeout=900,
                            creationflags=NO_WINDOW)
        jpath = os.path.splitext(work)[0] + "_judge.json"
        has_baked = False
        if os.path.exists(jpath):
            with open(jpath, encoding="utf-8") as f:
                rep = json.load(f)
            verdict = rep.get("verdict", "?")
            has_baked = bool(rep.get("has_subtitle"))
        if os.path.exists(jpath):
            note = ("评分 good · 自动放行" if verdict == "good"
                    else f"评分 {verdict} · 建议换种子重跑")
        else:
            err = (jr.stderr or "").strip().replace("\n", " ")[-80:]
            note = "评分未产出 · 放行" + (f"（{err}）" if err else "")
        stage_end(base, "qc", detail=note + (" · 自带字幕" if has_baked else ""))

        # ── 4 音色转换（用户 voice_manual；硬闸在 h3_voice 内，失败回退原音轨）──
        # 必须在字幕对齐之前完成，字幕按最终音色重新对齐；BGM 永远在音色之后。
        with _lock:
            _t = _tasks.get(base) or {}
        voice_manual = _t.get("voice_manual", "off")
        voice_info = {"status": "skipped", "reason": "voice_manual off"}
        if voice_manual != "on":
            stage_end(base, "voice", "skip", detail="音色开关未打开（按钮·关）· 跳过")
        else:
            stage_begin(base, "voice", "Seed-VC · 角色音色 · 硬闸校验")
            try:
                vh = _voice_mod()
                prof = vh.resolve_profile_for_video(base)
                plan = vh.plan_single(work, prof)
                if not plan.get("segments"):
                    voice_info = {"status": "skipped",
                                  "reason": plan.get("skip_reason") or "无可转换片段",
                                  "profile": (prof or {}).get("profile")}
                    stage_end(base, "voice", "skip",
                              detail="跳过 · " + str(voice_info["reason"])[:120])
                else:
                    stage_detail(base, "voice", "Seed-VC 转换 · "
                                 + "/".join(sorted({s.get("character", "") for s in plan["segments"]})))
                    res = vh.process_track(
                        work, prof, plan, WORK,
                        progress=lambda txt: stage_detail(base, "voice", txt),
                        log=lambda txt: log(f"[{base}] {txt}"),
                        expected_text="\n".join(
                            (l.get("bottom") or l.get("top") or "")
                            for l in (dlg.get("lines") or [])) or None)
                    voice_info = res
                    if res.get("output") and res.get("status") in ("done", "partial"):
                        os.replace(res["output"], work)  # 视频流 copy，仅换音轨
                        done_n = sum(1 for s in res.get("segments", [])
                                     if s.get("status") == "done")
                        detail = (f"{res.get('status')} · {done_n} 段 · 文本相似度 "
                                  f"{res.get('gates', {}).get('text_sim')} · "
                                  "F0/时长/峰值闸通过 · 48k 立体声")
                        seg_txt = res.get("gates", {}).get("text_sim_segments") or []
                        if seg_txt:
                            detail += " · 分段文本 " + "/".join(
                                f"{s.get('character')}:{s.get('text_sim')}"
                                for s in seg_txt)
                        if res.get("segment_fallback"):
                            detail += f" · 回退原音 {len(res['segment_fallback'])} 段"
                        if res.get("reason"):
                            detail += " · " + str(res["reason"])[:100]
                        stage_end(base, "voice", detail=detail)
                        ev(base, None, "音色转换 · " + detail)
                    else:
                        stage_end(base, "voice", "skip",
                                  detail="回退原音轨 · " + str(res.get("reason"))[:120])
            except Exception as e:
                voice_info = {"status": "skipped", "reason": str(e)[:200]}
                stage_end(base, "voice", "skip",
                          detail="回退原音轨 · " + str(e)[:120])
        with _lock:
            t = _tasks.get(base)
            if t:
                t["voice"] = voice_info

        # ── 5 字幕合成：按最终音色重新对齐（voice 之后、BGM 之前）──
        stage_begin(base, "subtitle", "Whisper · 按最终音色对齐")
        if has_baked:
            sub = work
            stage_end(base, "subtitle", "skip",
                      detail="H3 自带字幕 · 跳过后期烧录")
        else:
            stage_detail(base, "subtitle", "语音对齐 · 双语字幕烧录")
            sub = subtitle_video(work, lambda txt: stage_detail(base, "subtitle", txt),
                                 dec, dlg, style="h3")
            stage_end(base, "subtitle",
                      detail="烧录完成 · H3 样式（Noto Sans SC · 常规字重 · 底部偏上）")

        # 音色已做 loudnorm；跳过 enhance_voice_audio，避免二次增强/重复归一
        if voice_info.get("status") in ("done", "partial"):
            ev(base, None, "音色已转换 · 跳过音色增强（响度已归一）")
        elif enhance_voice_audio(sub):
            ev(base, None, "音色增强 · 响度归一 -16 LUFS")

        # ── 5 配乐混音（单条视频：按钮·开=让 JEV 决策；按钮·关=不配乐）──
        with _lock:
            _t = _tasks.get(base) or {}
        bgm_manual = _t.get("bgm_manual", "off")
        if bgm_manual != "on":
            stage_end(base, "bgm", "skip", detail="配乐开关未打开（按钮·关）· 跳过")
        elif dec.get("bgm") != "need":
            stage_end(base, "bgm", "skip", detail="JEV 判定无需配乐（按钮·开）· 跳过")
        else:
            stage_begin(base, "bgm", f"曲库检索 · 匹配 {MOOD_SHORT.get(dec.get('mood', ''), '')}")
            bgm_file = find_bgm()
            if bgm_file:
                stage_detail(base, "bgm", f"命中曲库 · {os.path.basename(bgm_file)}")
            else:
                stage_detail(base, "bgm", "曲库为空 · 唤起 ACE-Step 引擎")
                bgm_file = None
                if ensure_engine(180):
                    stage_detail(base, "bgm", "引擎在线 · 生成 "
                                 + MOOD_SHORT.get(dec.get("mood", ""), "") + " 曲目")
                    bgm_file = gen_bgm_track(dec.get("mood", "gentle"), bgm_type,
                                             probe_duration(sub) + 2)
                if not bgm_file:
                    stage_end(base, "bgm", "skip",
                              detail="音乐引擎未就绪/无产出 · 跳过配乐继续交付")
            if bgm_file:
                stage_detail(base, "bgm", "人声闪避混音 · BGM 22% 渐入")
                mixed = mix_bgm(sub, bgm_file, volume=dec.get("volume", 0.15),
                                duck=dec.get("duck", True))
                os.replace(mixed, sub)
                bgm_track_name = os.path.basename(bgm_file)
                stage_end(base, "bgm", detail="混音完成 · " + bgm_track_name)

        # ── 6 交付归档 ──
        stage_begin(base, "deliver", "成片落盘 · 原片归档")
        final = os.path.join(OUT, base)
        os.replace(sub, final)
        if jpath and os.path.exists(jpath):
            os.replace(jpath, os.path.splitext(final)[0] + "_judge.json")
        os.replace(src, os.path.join(ORIG, base))
        minutes = round((time.time() - t0) / 60, 1)
        stage_end(base, "deliver", detail=f"H:\\h3cache\\{base}")
        record_last_result(base, FONTS.get(dec.get("font", "雅黑"),
                                           FONTS["雅黑"])["family"],
                           dec, verdict, minutes)
        with _lock:
            t = _tasks.get(base)
            if t:
                t["state"] = "done"
                t["verdict"] = verdict
                t["output"] = final
                t["ended_at"] = time.time()
                t["pct"] = 100
        ev(base, None, f"已交付 H:\\h3cache\\{base} · 质检 {verdict} · 用时 {minutes} 分钟")
        log(f"[{base}] 交付完成 verdict={verdict} {minutes}min")
        _record_history(base, "done", t0, verdict, final, None,
                        dec, interp_on, bilingual, bgm_type, bgm_track_name,
                        bgm_manual, voice_manual, voice_info)
    except Exception as e:
        with _lock:
            t = _tasks.get(base)
            if t:
                if t.get("cur"):
                    st = t["stages"].get(t["cur"])
                    if st and st["state"] == "active":
                        st["state"] = "fail"
                        st["end"] = time.time()
                t["state"] = "error"
                t["error"] = str(e)[:200]
                t["ended_at"] = time.time()
        ev(base, None, "异常终止: " + str(e)[:120])
        log(f"[{base}] 异常: {e}")
        _record_history(base, "error", t0, verdict, None, str(e)[:200],
                        dec, interp_on, bilingual, bgm_type, bgm_track_name,
                        bgm_manual, voice_manual, voice_info)
        try:
            os.replace(src, os.path.join(FAIL, base))
            ev(base, None, "原片已隔离至 H:\\h3cache\\失败\\")
        except OSError:
            pass
    finally:
        for leftover in glob.glob(os.path.join(WORK, os.path.splitext(base)[0] + "*")):
            try:
                os.remove(leftover)
            except OSError:
                pass


def process_film(name):
    """长片全流程：分镜决策 → 逐段{生成→字幕→质检(换seed原位重跑≤2)} → 拼接 → 整片配乐 → 交付。
    与 process_video 完全独立（互不调用、互不共享状态）；重跑只在原 idx 原位替换，拼接顺序永不改变。
    交付：H:\\h3cache\\films\\<片名>\\seg_XX.mp4 + <片名>.mp4 + manifest.json"""
    src_json = os.path.join(RAW, name + ".film.json")
    fdir = os.path.join(FILMS, name)
    t0 = time.time()
    with _lock:
        f = _films.get(name)
        if f:
            f["state"] = "running"
            f["started_at"] = t0
            f["pos"] = None
            for other in list(_tasks.values()) + list(_films.values()):
                if other is not f and other.get("state") == "queued" \
                        and other.get("pos") and other["pos"] > 1:
                    other["pos"] -= 1
    ev(name + ".film", None, "长片开工 · 编排引擎就位")
    log(f"[{name}.film] 开始处理")
    try:
        with open(src_json, encoding="utf-8") as fjs:
            film = json.load(fjs)
        os.makedirs(fdir, exist_ok=True)
        style = str(film.get("style") or "通用长片")
        lang = str(film.get("lang") or "zh")
        voice_manual = str(film.get("voice") or film.get("voice_manual") or "off").lower()
        if voice_manual not in ("on", "off"):
            voice_manual = "off"
        voice_info = None
        bgm_track_name = None
        reuse = film.get("reuse") if isinstance(film.get("reuse"), list) else []
        lines = []
        for l in (film.get("lines") or []):
            if isinstance(l, dict):
                lines.append({"top": str(l.get("top") or ""),
                              "bottom": str(l.get("bottom") or "")})
            else:
                lines.append({"top": "", "bottom": str(l)})
        lines = [l for l in lines if (l["bottom"] or l["top"]).strip()]

        # ── 1 分镜决策（JEV：是否分镜/段间转场/风格锚/字体/整片配乐；切分≤3行/段确定性）──
        film_set(name, phase="decide", phase_text="JEV-4B 分镜推理 · 转场/风格锚/配乐")
        sj = os.path.join(fdir, "_film.state.json")
        dpath = os.path.join(fdir, "_film.storyboard.json")
        try:
            with open(sj, "w", encoding="utf-8") as f2:
                json.dump({"style": style, "lang": lang,
                           "lines": [l["bottom"] or l["top"] for l in lines]}, f2,
                          ensure_ascii=False)
            r = subprocess.run([JEV_PY, STORYBOARD_SCRIPT, sj, dpath],
                               capture_output=True, text=True, timeout=600,
                               creationflags=NO_WINDOW)
            if not os.path.exists(dpath):
                raise RuntimeError("分镜决策未产出: " + (r.stderr or "")[-120:])
            with open(dpath, encoding="utf-8") as f2:
                sb = json.load(f2)
        finally:
            for p in (sj, dpath):
                try:
                    os.remove(p)
                except OSError:
                    pass
        segments = sb.get("segments") or []
        if not sb.get("need_storyboard", True) or not segments:
            segments = [{"idx": 0, "scene_prompt": "", "lines": lines,
                         "transition_to_next": "cut"}]  # 单片模式：全部台词一段
        with _lock:
            f = _films.get(name)
            if f:
                f["segments"] = [{"idx": i, "state": "pending", "retries": 0,
                                  "verdict": None, "download": None,
                                  "note": "", "error": None}
                                 for i in range(len(segments))]
                f["need_storyboard"] = bool(sb.get("need_storyboard", True))
                f["style_anchor"] = sb.get("style_anchor") or style
                f["pct"] = _film_pct(f)
        dec = sb  # subtitle_video 只消费 font 决策，直接复用
        film_set(name, phase_text=f"分镜就绪 · {len(segments)} 段 · 风格锚已定"
                  + ("（降级默认）" if sb.get("degraded") else ""))
        ev(name + ".film", "decide",
           (f"分镜 {len(segments)} 段 · 转场 "
            + "/".join((segments[i].get("transition_to_next") or "cut")
                       for i in range(len(segments) - 1)))
           if len(segments) > 1 else f"单片模式 · 共 {len(lines)} 行台词")

        # reuse 多于段数：告警但不改段数（段数由台词行数/分镜决策决定；多余项静默丢弃）
        if reuse and len(reuse) > len(segments):
            warn = (f"reuse {len(reuse)} 项多于段数 {len(segments)}，"
                    f"多余 {len(reuse) - len(segments)} 项未使用")
            ev(name + ".film", None, "⚠ " + warn)
            log(f"[{name}.film] ⚠ {warn}")
            film_set(name, note="⚠ " + warn)

        manifest_segs = []
        guard_prof = film_guard_profile(name)
        if guard_prof and not reuse:
            ev(name + ".film", "guard", "守卫 profile 就绪 · "
               + os.path.basename(guard_prof) + " · 生成前注入")

        def write_manifest(concat_info=None, output=None):
            """追溯清单：每段落盘一次，异常吞掉不打断主流程"""
            try:
                with _lock:
                    f = _films.get(name) or {}
                    seg_snap = [dict(s) for s in f.get("segments") or []]
                manifest = {
                    "name": name, "style": style, "lang": lang,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "need_storyboard": bool(sb.get("need_storyboard", True)),
                    "style_anchor": sb.get("style_anchor"),
                    "decision": {k: sb.get(k) for k in
                                 ("font", "bgm", "bgm_type", "mood", "degraded")},
                    "reference_image": film.get("reference_image"),
                    "reuse": reuse or None,
                    "guard_profile": os.path.basename(guard_prof) if guard_prof else None,
                    "segments": manifest_segs,
                    "live_segments": seg_snap,
                    "concat": concat_info,
                    "voice": voice_info,
                    "bgm_track": bgm_track_name,
                    "output": output,
                }
                with open(os.path.join(fdir, "manifest.json"), "w",
                          encoding="utf-8") as fm:
                    json.dump(manifest, fm, ensure_ascii=False, indent=1)
            except Exception:
                pass

        # ── 2 逐段循环：生成 → 字幕 → 质检（不过关换 seed 原位重跑，≤FILM_MAX_RETRIES 次）──
        film_set(name, phase="generate")
        ref_name = None
        for i, seg in enumerate(segments):
            seg_lines = seg.get("lines") or []
            prompt_txt = (seg.get("scene_prompt")
                          or sb.get("style_anchor") or style)
            retries = 0
            seg_t0 = time.time()  # 本段墙钟起点（含重跑）：ETA 用已完成段的实际耗时
            film_seg_set(name, i, seg_started_at=seg_t0)
            sub = None
            verdict = "?"
            has_baked = False  # H3 是否已自带字幕（由 JEV 裁判在烧录前检测）
            pad_crop = None  # (w,h,x,y) 垫黑边段的内容区，送检前裁掉黑边
            while True:
                film_seg_set(name, i, state="gen", error=None,
                             note="ComfyUI 出片中" if not reuse else "reuse 回放")
                if reuse:
                    if i >= len(reuse):
                        raise RuntimeError(f"reuse 列表仅 {len(reuse)} 项，缺第 {i} 段")
                    rp = reuse[i]
                    if not os.path.isfile(rp):
                        raise RuntimeError(f"reuse[{i}] 文件不存在: {rp}")
                    raw = os.path.join(fdir, f"seg_{i:02d}_raw.mp4")
                    shutil.copy2(rp, raw)
                    src_info = {"kind": "reuse", "path": rp}
                    seed = None
                else:
                    # 首帧链：段0 用参考图；后续取上段 raw 末帧（字幕前画面，防字幕污染下段首帧）
                    if i == 0:
                        if ref_name is None:
                            ref_name = film_resolve_reference(
                                name, film.get("reference_image"))
                    else:
                        png = film_last_frame(
                            os.path.join(fdir, f"seg_{i - 1:02d}_raw.mp4"),
                            os.path.join(fdir, f"seed_{i:02d}.png"))
                        dst_png = os.path.join(
                            COMFY_INPUT,
                            "film_" + _film_safe(name) + f"_s{i:02d}_seed.png")
                        shutil.copy2(png, dst_png)
                        ref_name = os.path.basename(dst_png)
                    seed = random.randint(0, 2 ** 48 - 1)
                    g = film_generate_segment(name, i, film_guard_prompt(
                        prompt_txt, guard_prof, i, repair=retries > 0),
                                              seed, ref_name, fdir)
                    src_info = {"kind": "generated", "prompt_id": g["prompt_id"]}
                if i > 0:  # 段间归一化先行：字幕烧在统一画幅上，全片字号一致
                    pad_crop = None
                    a0 = os.path.join(fdir, "seg_00.mp4")
                    rawp = os.path.join(fdir, f"seg_{i:02d}_raw.mp4")
                    if os.path.isfile(a0) and os.path.isfile(rawp):
                        aw, ah = probe_media(a0)
                        sw, sh = probe_media(rawp)
                        if (sw, sh) != (aw, ah) or probe_audio(rawp) != "48000:2":
                            fps = film_probe_fps(a0) or 24
                            nrm = os.path.join(fdir, f"seg_{i:02d}_norm.mp4")
                            # 同帧率不再套 fps=N（会因时间戳舍入丢末帧），
                            # passthrough 保证归一化重编码帧数不变
                            vfn = (f"scale={aw}:{ah}:force_original_aspect_ratio=decrease,"
                                   f"pad={aw}:{ah}:(ow-iw)/2:(oh-ih)/2,setsar=1")
                            src_fps = film_probe_fps(rawp)
                            if src_fps and abs(src_fps - fps) > 0.01:
                                vfn += f",fps={fps}"
                            rn = subprocess.run(
                                [FFMPEG, "-y", "-i", rawp, "-vf", vfn,
                                 "-fps_mode", "passthrough",
                                 "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-profile:v", "main", "-level", "4.0",
                                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000", nrm],
                                cwd=fdir, capture_output=True, text=True,
                                creationflags=NO_WINDOW)
                            if rn.returncode != 0 or not os.path.isfile(nrm):
                                raise RuntimeError(
                                    f"seg_{i:02d} 归一化失败: " + (rn.stderr or "")[-160:])
                            os.replace(nrm, rawp)
                            if (sw, sh) != (aw, ah):  # 仅画面垫过边才需要裁边送检
                                fct = min(aw / sw, ah / sh)  # 内容区在垫边帧里的位置尺寸
                                cw2, ch2 = round(sw * fct), round(sh * fct)
                                pad_crop = (cw2, ch2, (aw - cw2) // 2, (ah - ch2) // 2)
                if i == 0:  # 首段音频统一 48k 立体声：concat -c copy 不容采样率混排
                    rawp0 = os.path.join(fdir, "seg_00_raw.mp4")
                    if os.path.isfile(rawp0) and probe_audio(rawp0) not in (None, "48000:2"):
                        a0n = os.path.join(fdir, "seg_00_anorm.mp4")
                        ra = subprocess.run(
                            [FFMPEG, "-y", "-i", rawp0, "-c:v", "copy",
                             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", a0n],
                            cwd=fdir, capture_output=True, text=True,
                            creationflags=NO_WINDOW)
                        if ra.returncode != 0 or not os.path.isfile(a0n):
                            raise RuntimeError(
                                "seg_00 音频统一失败: " + (ra.stderr or "")[-160:])
                        os.replace(a0n, rawp0)
                rawp = os.path.join(fdir, f"seg_{i:02d}_raw.mp4")
                film_seg_set(name, i, state="qc", note="JEV 抽帧评分 · 检测自带字幕")
                judge_target = rawp
                if pad_crop:  # 垫黑边段裁回内容区送检：黑边属拼接适配，不应拉低评分
                    cw2, ch2, cx, cy = pad_crop
                    jt = os.path.join(fdir, f"seg_{i:02d}_judge_view.mp4")
                    rj = subprocess.run(
                        [FFMPEG, "-y", "-i", rawp, "-vf",
                         f"crop={cw2}:{ch2}:{cx}:{cy}", "-an",
                         "-c:v", "libx264", "-preset", "fast", "-crf", "20", jt],
                        cwd=fdir, capture_output=True, text=True,
                        creationflags=NO_WINDOW)
                    if rj.returncode == 0 and os.path.isfile(jt):
                        judge_target = jt
                jr = subprocess.run([JEV_PY, JUDGE_SCRIPT, "--video", judge_target],
                                    capture_output=True, text=True, timeout=900,
                                    creationflags=NO_WINDOW)
                if judge_target != rawp:
                    try:
                        os.remove(judge_target)
                    except OSError:
                        pass
                jpath = os.path.splitext(judge_target)[0] + "_judge.json"
                verdict = "?"
                if os.path.exists(jpath):
                    with open(jpath, encoding="utf-8") as fh:
                        rep = json.load(fh)
                    verdict = rep.get("verdict", "?")
                    has_baked = bool(rep.get("has_subtitle"))
                    try:
                        os.remove(jpath)
                    except OSError:
                        pass
                else:
                    verdict = "?"  # 评分未产出视为放行（与 process_video 同策略）
                if has_baked:
                    film_seg_set(name, i, state="sub",
                                 note="H3 自带字幕 · 跳过字幕烧录")
                    ev(name + ".film", "subtitle",
                       f"seg_{i:02d} 检测到 H3 自带字幕，跳过后期烧录")
                    sub = rawp
                else:
                    film_seg_set(name, i, state="sub", note="语音对齐 · 字幕烧录")
                    sub = subtitle_video(rawp,
                                         lambda txt, _i=i: film_seg_set(
                                             name, _i, state="sub", note=txt),
                                         dec, {"lang": lang, "lines": seg_lines},
                                         style="h3")
                film_seg_set(name, i, state="qc",
                             note=f"质检 {verdict}" + (" · 自带字幕" if has_baked else ""))
                if verdict in ("good", "?") or reuse or retries >= FILM_MAX_RETRIES:
                    break  # reuse 素材重跑=复制同一文件，无增益：接受首判
                retries += 1
                film_seg_set(name, i, retries=retries, state="gen",
                             note=f"评分 {verdict} · 换 seed 原位重跑（第 {retries} 次）")
                ev(name + ".film", "generate",
                   f"seg_{i:02d} 评分 {verdict}，换 seed 原位重跑（第 {retries} 次）")
            final = os.path.join(fdir, f"seg_{i:02d}.mp4")
            if os.path.abspath(sub) == os.path.abspath(rawp):
                shutil.copy2(sub, final)  # 自带字幕：复制入片，raw 留给下段抽末帧
            else:
                os.replace(sub, final)  # 原位替换：同 idx 覆盖，拼接顺序永不改变
            audio_enhanced = bool(enhance_voice_audio(final))
            if audio_enhanced:
                ev(name + ".film", "audio", f"seg_{i:02d} 音色增强 · 响度归一")
            dur_s = probe_duration(final)
            w, h = probe_media(final)
            film_seg_set(name, i, state="done", verdict=verdict, note="",
                         elapsed_s=round(time.time() - seg_t0, 1),
                         download=f"{name}/seg_{i:02d}.mp4")
            manifest_segs.append({
                "idx": i, "file": os.path.basename(final),
                "duration_s": round(dur_s, 2), "width": w, "height": h,
                "seed": seed, "retries": retries, "verdict": verdict,
                "baked_subtitle": has_baked,
                "audio_enhanced": audio_enhanced,
                "transition_to_next": seg.get("transition_to_next") or "cut",
                "source": src_info, "lines": len(seg_lines)})
            write_manifest()
            ev(name + ".film", "generate",
               f"seg_{i:02d} 完成 · 质检 {verdict} · {dur_s:.1f}s"
               + (f" · 重跑 {retries} 次" if retries else ""))

        # ── 3 拼接：全 cut 且参数一致 → -c copy；含 crossfade → xfade 链；不一致 → 归一化再拼 ──
        film_set(name, phase="concat", phase_text="段参数一致性校验")
        finals = [os.path.join(fdir, f"seg_{i:02d}.mp4")
                  for i in range(len(segments))]
        uniform, dims, durs = film_probe_uniform(finals)
        transitions = [(segments[i].get("transition_to_next") or "cut")
                       for i in range(len(segments))]
        has_xfade = len(finals) > 1 and any(
            t == "crossfade" for t in transitions[:-1])
        out_film = os.path.join(fdir, name + ".mp4")
        if len(finals) == 1:
            shutil.copy2(finals[0], out_film)
            concat_info = {"method": "single", "uniform": True}
        elif not uniform:
            film_set(name, phase_text="段参数不一致 · 归一化重编码后拼接")
            norm = film_normalize(finals, fdir)
            film_concat_copy(norm, out_film)
            concat_info = {"method": "reencode+concat-copy", "uniform": False,
                           "dims": [list(d) for d in dims]}
        elif has_xfade:
            film_set(name, phase_text="含交叉淡化 · xfade/acrossfade 链重编码")
            film_concat_xfade(finals, transitions, out_film)
            concat_info = {"method": "xfade", "uniform": True,
                           "xfade_s": 0.5, "durations": durs}
        else:
            film_concat_copy(finals, out_film)
            concat_info = {"method": "concat-copy", "uniform": True,
                           "durations": durs}
        film_set(name, phase_text=f"拼接完成 · {concat_info['method']}"
                  f" · 总长 {probe_duration(out_film):.1f}s")

        # ── 3.5 整片音色：只在 concat 之后、BGM 之前；绝不改 seg_XX 子视频 ──
        film_set(name, phase="voice", phase_text="音色转换 · 角色参考音")
        if voice_manual != "on":
            voice_info = {"status": "skipped", "reason": "voice_manual off"}
            film_set(name, voice=voice_info, phase_text="音色开关未打开 · 跳过")
        else:
            try:
                vh = _voice_mod()
                hint = film.get("voice_profile")
                prof = vh.load_profile(hint) if hint else vh.resolve_profile_for_film(name)
                # manifest 段边界/时间范围（xfade 段间重叠 0.5s，按实际拼接语义折算）
                xf = 0.5 if concat_info.get("method") == "xfade" else 0.0
                cursor = 0.0
                for i, ms in enumerate(manifest_segs):
                    d = float(ms.get("duration_s") or 0)
                    ms["start_s"] = round(cursor, 3)
                    ms["end_s"] = round(cursor + d, 3)
                    cursor = cursor + d - (xf if i < len(manifest_segs) - 1 else 0)
                    srcp = ""
                    if isinstance(ms.get("source"), dict):
                        srcp = ms["source"].get("path") or ms["source"].get("file") or ""
                    m = re.search(r"seg[_\-]?(\d+)", os.path.basename(str(srcp)), re.I)
                    if m:
                        ms["seg_no"] = int(m.group(1))
                    sp = ((((prof or {}).get("segments") or {})
                           .get(str(ms.get("seg_no", i + 1))) or {})
                          .get("speakers") or [])
                    ms["speakers"] = sp
                plan = vh.plan_film(prof, manifest_segs, concat_info,
                                    probe_duration(out_film))
                if not plan.get("segments"):
                    voice_info = {"status": "skipped",
                                  "reason": plan.get("skip_reason") or "无可靠片段",
                                  "profile": (prof or {}).get("profile"),
                                  "segments": [], "skipped": plan.get("skipped") or []}
                    film_set(name, voice=voice_info,
                             phase_text="跳过 · " + str(voice_info["reason"])[:100])
                    ev(name + ".film", "voice",
                       "音色跳过 · " + str(voice_info["reason"])[:120])
                else:
                    sub_before = {os.path.basename(p): _sha256(p)
                                  for p in finals if os.path.isfile(p)}
                    res = vh.process_track(
                        out_film, prof, plan, fdir,
                        progress=lambda txt: film_set(name, phase_text=txt),
                        log=lambda txt: log(f"[{name}.film] {txt}"),
                        expected_text="\n".join(
                            (l.get("bottom") or l.get("top") or "")
                            for l in lines) or None)
                    sub_after = {os.path.basename(p): _sha256(p)
                                 for p in finals if os.path.isfile(p)}
                    res["subvideos_unchanged"] = bool(sub_before == sub_after)
                    voice_info = res
                    if (res.get("output") and res.get("status") in ("done", "partial")
                            and res["subvideos_unchanged"]):
                        os.replace(res["output"], out_film)  # 视频流 copy，仅换音轨
                        detail = (f"{res.get('status')} · 文本 "
                                  f"{res.get('gates', {}).get('text_sim')} · "
                                  "48k 立体声 · 子视频未改")
                        seg_txt = res.get("gates", {}).get("text_sim_segments") or []
                        if seg_txt:
                            detail += " · 分段文本 " + "/".join(
                                f"{s.get('character')}:{s.get('text_sim')}"
                                for s in seg_txt)
                        if res.get("segment_fallback"):
                            detail += f" · 回退原音 {len(res['segment_fallback'])} 段"
                        film_set(name, voice=voice_info, phase_text="音色完成 · " + detail)
                        ev(name + ".film", "voice", "音色转换 · " + detail)
                    else:
                        if res.get("output") and not res["subvideos_unchanged"]:
                            res["reason"] = "子视频被改动，回退原音轨"
                        try:
                            if res.get("output"):
                                os.remove(res["output"])
                        except OSError:
                            pass
                        film_set(name, voice=voice_info,
                                 phase_text="回退原音轨 · " + str(res.get("reason"))[:100])
                        ev(name + ".film", "voice",
                           "音色回退 · " + str(res.get("reason"))[:120])
            except Exception as e:
                voice_info = {"status": "skipped", "reason": str(e)[:200]}
                film_set(name, voice=voice_info, phase_text="音色跳过 · " + str(e)[:100])
                ev(name + ".film", "voice", "音色跳过 · " + str(e)[:120])

        # ── 4 整片配乐：BGM 永远在音色之后；绝不转换 BGM ──
        bgm_dec = (film.get("bgm") if str(film.get("bgm")).lower() in ("need", "none")
                   else sb.get("bgm", "need"))
        if str(bgm_dec).lower() == "none":
            film_set(name, phase="music", phase_text="JEV 判定无需配乐 · 跳过")
        else:
            mood = film.get("mood") or sb.get("mood") or "gentle"
            btype = str(film.get("bgm_type") or sb.get("bgm_type", "instrumental")).lower()
            film_set(name, phase="music",
                     phase_text=f"曲库检索 · 匹配 {MOOD_SHORT.get(mood, mood)}")
            bgm_file = find_bgm()
            if bgm_file:
                film_set(name, phase_text=f"命中曲库 · {os.path.basename(bgm_file)}")
            else:
                film_set(name, phase_text="曲库为空 · 唤起 ACE-Step 引擎")
                bgm_file = None
                if ensure_engine(180):
                    film_set(name, phase_text="引擎在线 · 生成 "
                             + MOOD_SHORT.get(mood, "") + " 曲目")
                    bgm_file = gen_bgm_track(mood, btype,
                                             probe_duration(out_film) + 2)
                if not bgm_file:
                    film_set(name, phase="music",
                             phase_text="音乐引擎未就绪/无产出 · 跳过配乐继续交付")
            if bgm_file:
                bgm_track_name = os.path.basename(bgm_file)
                film_set(name, phase_text="整片一次混入 · 人声闪避")
                mixed = mix_bgm(out_film, bgm_file, volume=0.15, duck=True)
                os.replace(mixed, out_film)

        # ── 5 交付：整片 + seg_XX + manifest 归档，订单挪出 raw ──
        film_set(name, phase="deliver", phase_text="manifest 落盘 · 归档")
        write_manifest(concat_info, out_film)
        minutes = round((time.time() - t0) / 60, 1)
        with _lock:
            f = _films.get(name)
            if f:
                f["state"] = "done"
                f["output"] = out_film
                f["ended_at"] = time.time()
                f["pct"] = 100
                f["phase_text"] = (f"已交付 H:\\h3cache\\films\\{name}\\{name}.mp4"
                                   f" · 用时 {minutes} 分钟")
        os.replace(src_json, os.path.join(ORIG, name + ".film.json"))
        ev(name + ".film", None, f"长片交付完成 · {len(segments)} 段 · 用时 {minutes} 分钟")
        log(f"[{name}.film] 交付完成 {minutes}min")
        try:
            with _lock:
                seg_snap = [dict(s) for s in (_films.get(name) or {}).get("segments") or []]
            _append_history({
                "name": name + ".film", "film": True, "state": "done",
                "finished_at": time.time(), "started_at": t0,
                "duration_s": round(time.time() - t0, 1),
                "verdict": _worst_verdict([s.get("verdict") for s in seg_snap]),
                "output": out_film, "segments": seg_snap,
                "style_anchor": sb.get("style_anchor"),
                "voice": voice_info,
                "decisions": {"font": sb.get("font"), "bgm": sb.get("bgm"),
                              "bgm_type": sb.get("bgm_type"), "mood": sb.get("mood"),
                              "need_storyboard": sb.get("need_storyboard"),
                              "voice_manual": voice_manual,
                              "bgm_track": bgm_track_name,
                              "concat": concat_info.get("method")}})
        except Exception:
            pass
    except Exception as e:
        film_set(name, state="error", error=str(e)[:200], ended_at=time.time(),
                 phase_text="异常终止: " + str(e)[:80])
        ev(name + ".film", None, "长片异常终止: " + str(e)[:120])
        log(f"[{name}.film] 异常: {e}")
        try:
            with _lock:
                seg_snap = [dict(s) for s in (_films.get(name) or {}).get("segments") or []]
            _append_history({
                "name": name + ".film", "film": True, "state": "error",
                "finished_at": time.time(), "started_at": t0,
                "duration_s": round(time.time() - t0, 1),
                "verdict": _worst_verdict([s.get("verdict") for s in seg_snap]),
                "output": None, "segments": seg_snap,
                "voice": voice_info,
                "error": str(e)[:200]})
        except Exception:
            pass
        try:
            os.replace(src_json, os.path.join(FAIL, name + ".film.json"))
            ev(name + ".film", None, "长片订单已隔离至 H:\\h3cache\\失败\\")
        except OSError:
            pass
    finally:
        for pat in ("seg_*_raw.mp4", "seed_*.png", "norm_*.mp4", "*_list.txt",
                    "*_judge.json", "_sub_temp.ass", "*.16k.wav", "*_bgm.mp4",
                    "*_voiced.mp4"):
            for leftover in glob.glob(os.path.join(fdir, pat)):
                try:
                    os.remove(leftover)
                except OSError:
                    pass


def worker():
    """单工作线程 FIFO：串行执行防显存/内存打满；线路状态彼此独立。
    .film.json 后缀 → 长片编排；其余 → 单片流水线（原有路径不变）"""
    while True:
        with _queue_cv:
            while not _queue:
                _queue_cv.wait(5)
            base = _queue.pop(0)
        if base.endswith(".film.json"):
            try:
                process_film(base[:-len(".film.json")])
            finally:
                # 订单已归档/进 FAIL；解除同名拉黑，重投可再触发
                with _lock:
                    _film_registered.discard(base)
        else:
            try:  # 视频开工前同步推进排队长片的位次展示（process_video 只管 _tasks）
                with _lock:
                    for fl in _films.values():
                        if fl["state"] == "queued" and fl["pos"] and fl["pos"] > 1:
                            fl["pos"] -= 1
            except Exception:
                pass
            process_video(base)


def detector():
    """扫描 raw 目录：文件大小连续两轮稳定 → 入列"""
    global _overall
    sizes, registered = {}, set()
    while True:
        running = queued = 0
        with _lock:
            running = (sum(1 for t in _tasks.values() if t["state"] == "running")
                       + sum(1 for x in _films.values() if x["state"] == "running"))
            queued = len(_queue)
        if running:
            _overall = {"phase": "busy", "text": "制作进行中"}
        elif queued:
            _overall = {"phase": "busy", "text": f"队列待处理 · {queued} 个任务"}
        else:
            busy = comfy_busy()
            if busy:
                _overall = {"phase": "generating", "text": "ComfyUI 生成中 · 出片后自动开工"}
            else:
                _overall = {"phase": "idle", "text": "待片中 · 盯住 raw 目录"}

        for mp4 in glob.glob(os.path.join(RAW, "*.mp4")):
            base = os.path.basename(mp4)
            if base.startswith(FILM_SEG_PREFIX):
                continue  # 长片段产物（filmseg_ 前缀）由编排器独占消费，不走单片流水线
            if base in registered:
                continue
            try:
                sz = os.path.getsize(mp4)
            except OSError:
                continue
            prev = sizes.get(mp4)
            sizes[mp4] = sz
            if prev is not None and sz == prev and sz > 1_000_000:
                registered.add(base)
                with _queue_cv:
                    _queue.append(base)
                    task_create(base, len(_queue))
                    _queue_cv.notify()
                log(f"[{base}] 检测到稳定出片，入列")

        # 长片触发：raw\<片名>.film.json 落盘且解析通过 → 入列（与单片同 FIFO，互不干扰）
        try:
            for fj in glob.glob(os.path.join(RAW, "*.film.json")):
                fbase = os.path.basename(fj)
                if fbase in _film_registered:
                    continue
                try:
                    with open(fj, encoding="utf-8") as fh:
                        tmp = json.load(fh)
                    if not isinstance(tmp, dict) \
                            or not isinstance(tmp.get("lines"), list):
                        continue  # 半写/结构不对 → 下轮再看
                except Exception:
                    continue
                fname = fbase[:-len(".film.json")]
                _film_registered.add(fbase)
                with _queue_cv:
                    _queue.append(fbase)
                    film_create(fname, len(_queue))
                    _queue_cv.notify()
                log(f"[{fbase}] 检测到长片订单，入列")
        except Exception:
            pass  # 长片扫描异常绝不影响单片检测
        time.sleep(POLL)


def approved_watcher():
    """审核通过目录 → 自动导入 ComfyUI 图片输入库"""
    src_dir = r"H:\h3cache\approved"
    dst_dir = r"D:\ComfyUI\input"
    os.makedirs(src_dir, exist_ok=True)
    seen = set()
    while True:
        try:
            for f in (glob.glob(os.path.join(src_dir, "*.png"))
                      + glob.glob(os.path.join(src_dir, "*.jpg"))
                      + glob.glob(os.path.join(src_dir, "*.webp"))):
                if f in seen:
                    continue
                seen.add(f)
                dst = os.path.join(dst_dir, "审核_" + os.path.basename(f))
                shutil.copy2(f, dst)
                ev("系统", None, f"审核图已入库: {os.path.basename(dst)}（LoadImage 下拉可选）")
        except Exception:
            pass
        time.sleep(4)


def main():
    try:
        srv = SingleInstanceHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        log("导演台已在运行（端口被占），本实例退出")
        return
    _load_eta()
    _load_history()
    for d in (RAW, ORIG, WORK, FAIL, FILMS, DOWNLOADS, CACHE):
        os.makedirs(d, exist_ok=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=approved_watcher, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    log(f"H3 导演台启动 · http://127.0.0.1:{PORT}")
    detector()


if __name__ == "__main__":
    main()
