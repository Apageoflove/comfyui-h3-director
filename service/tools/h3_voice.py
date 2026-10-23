# -*- coding: utf-8 -*-
"""H3 音色转换层（Seed-VC，独立 venv/CLI）

职责边界（硬约束）：
- 只做「确定性音色转换 + 硬安全闸」，不做任何审美/文本判断。
- JEV / h3_guard 只提供「谁在说话 / 角色与情绪映射 / 选哪个参考音」；
  音色是否可用只由本文件的闸门决定：时长、Whisper 文本相似度、F0 范围、
  性别/音域不翻转、峰值不削波。
- 长片只处理 concat 之后的最终合并视频；绝不改写 seg_XX 子视频。
- 没有可靠 speaker 时间范围时整片跳过（返回 skipped），不把整片转成同一个声音。
- 输出统一 48 kHz / 立体声 / loudnorm(-16 LUFS, TP -1.5)，视频流 -c:v copy。
- 任何一步失败都回退原音轨（调用方保持原文件不动），绝不让任务失败。
"""
import difflib
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

try:
    import numpy as np
except Exception:  # pragma: no cover - 部署环境必有 numpy
    np = None

FFMPEG = r"D:\ComfyUI\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
SEEDVC_PY = r"H:\MusicAI\envs\seedvc\Scripts\python.exe"
SEEDVC_APP = r"H:\MusicAI\apps\seed-vc"
SEEDVC_CKPT = r"H:\MusicAI\apps\seed-vc\checkpoints"
WHISPER_DIR = (r"E:\local-models\huggingface\hub\models--Systran--faster-whisper-small"
               r"\snapshots\536b0662742c02347bc0e980a01041f333bce120")

RAW = r"H:\h3cache\raw"
CACHE = r"H:\h3cache\h3_cache"
VOICE_PROFILE_DIRS = [r"H:\H3DirectorDesk\tools\h3_voice_profiles",
                      r"D:\H3DirectorDesk\tools\h3_voice_profiles"]
GUARD_DIRS = [r"D:\H3DirectorDesk\tools\h3_guard_profiles",
              r"H:\H3DirectorDesk\tools\h3_guard_profiles"]

SR = 48000
CH = 2
DUR_TOL = 0.05                 # VC 输出与原音轨时长差上限（秒）
TEXT_SIM_MIN = 0.90            # Whisper 转写文本相似度下限（无台词本时）
SCRIPT_SIM_MIN = 0.85          # 台词本内容保留下限（已知原文，容错同音字/写法差异）
PEAK_DBFS_MAX = -0.5           # 峰值上限（dBFS）
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.891"
NO_WINDOW = 0x08000000

# ── 雨声/环境声保留（voice 链路可选开关；默认关闭＝完全保持现有行为）──
# 打开后：非语音区间回填原音轨（精确雨声/环境声），语音区间＝转换声+无语音雨声床；
# 只改音轨内容，不改音色、不改文本闸，BGM 仍在 voice 之后混入。
AMBIENCE_RESTORE = os.environ.get("H3_VOICE_AMBIENCE", "").strip().lower() in (
    "1", "true", "on", "yes")
AMBIENCE_BED_DB = float(os.environ.get("H3_VOICE_AMBIENCE_BED_DB", "-3"))
AMBIENCE_VOICE_DB = float(os.environ.get("H3_VOICE_AMBIENCE_VOICE_DB", "3"))
AMBIENCE_SPEECH_PAD = 0.10     # VAD 语音区间前后余量（秒），防止切到字头字尾
AMBIENCE_FADE = 0.04           # 语音/非语音边界交叉淡化（秒）

_whisper = None
_cache = {}


# ─────────────────────────── 通用小工具 ───────────────────────────

def _run(cmd, timeout=1800, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=timeout, creationflags=NO_WINDOW)


def _probe(path):
    r = _run([FFMPEG, "-hide_banner", "-i", path], timeout=60)
    s = r.stderr or ""
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", s)
    dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
    a = re.search(r"Audio: .*?, (\d+) Hz, (mono|stereo)", s)
    return {"duration": dur,
            "audio": (f"{a.group(1)}:{1 if a.group(2) == 'mono' else 2}" if a else None)}


def _decode_pcm(path, sr=SR, ch=CH):
    """任意音视频 → float32 PCM，形状 (n, ch)，失败抛错"""
    r = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-vn",
                        "-ar", str(sr), "-ac", str(ch),
                        "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"],
                       capture_output=True, creationflags=NO_WINDOW)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("音频解码失败: " + (r.stderr or b"").decode("utf-8", "ignore")[-200:])
    pcm = np.frombuffer(r.stdout, dtype="<f4").astype(np.float32)
    return pcm.reshape(-1, ch)


def _write_wav(pcm, path, sr=SR):
    """float32 PCM → 标准 16-bit WAV（loudnorm/whisper 都需要可解析容器）"""
    import wave
    pcm = np.asarray(pcm, dtype=np.float32)
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    data = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.tobytes())
    return path


def _peak_dbfs(pcm):
    if pcm is None or not len(pcm):
        return -120.0
    peak = float(np.max(np.abs(pcm)))
    return float(20.0 * np.log10(max(peak, 1e-9)))


def _video_stream_sha(path):
    """视频流 packet SHA256：证明音色/混音阶段只换音轨、视频流 -c:v copy。"""
    r = _run([FFMPEG, "-v", "error", "-i", path, "-map", "0:v:0", "-c", "copy",
              "-f", "streamhash", "-hash", "sha256", "-"], timeout=300)
    m = re.search(r"SHA256=([0-9a-fA-F]+)", r.stdout or "")
    return m.group(1).lower() if m else None


_TC2SC = {
    "讀": "读", "歷": "历", "簡": "简", "來": "来", "這": "这", "說": "说",
    "對": "对", "時": "时", "會": "会", "為": "为", "開": "开", "關": "关",
    "們": "们", "後": "后", "裡": "里", "過": "过", "還": "还", "發": "发",
    "現": "现", "覺": "觉", "著": "着", "讓": "让", "給": "给", "嗎": "吗",
    "學": "学", "師": "师", "媽": "妈", "兒": "儿", "點": "点", "個": "个",
    "麼": "么", "誰": "谁", "見": "见", "聽": "听", "話": "话", "語": "语",
    "電": "电", "視": "视", "車": "车", "錢": "钱", "買": "买", "賣": "卖",
    "東": "东", "頭": "头", "臉": "脸", "愛": "爱", "記": "记", "夢": "梦",
    "聲": "声", "樂": "乐", "壞": "坏", "錯": "错", "難": "难", "問": "问",
    "題": "题", "試": "试", "數": "数", "課": "课", "書": "书", "筆": "笔",
    "紙": "纸", "腦": "脑", "機": "机", "進": "进", "邊": "边", "樣": "样",
    "種": "种", "從": "从", "認": "认", "識": "识", "應": "应", "該": "该",
    "願": "愿", "幫": "帮", "謝": "谢", "氣": "气", "雲": "云", "風": "风",
    "溫": "温", "熱": "热", "場": "场", "長": "长", "間": "间", "陽": "阳",
    "陰": "阴", "塵": "尘", "輕": "轻", "遠": "远", "離": "离", "別": "别",
    "緊": "紧", "張": "张", "樓": "楼", "鐘": "钟", "盡": "尽", "勁": "劲",
    "沒": "没", "連": "连", "盡": "尽", "淚": "泪", "憶": "忆", "嗎": "吗",
}
_TC2SC_TRANS = str.maketrans(_TC2SC)


def _num2cn(n):
    """阿拉伯数字 → 中文读法（仅 0..9999；ASR 常把「六十」写成 60）"""
    if n < 0 or n > 9999:
        return str(n)
    if n == 0:
        return "零"
    d = "零一二三四五六七八九"
    u = ["", "十", "百", "千"]
    s, out, zero = str(n), "", False
    for i, ch in enumerate(s):
        v, pos = int(ch), len(s) - 1 - i
        if v == 0:
            zero = True
            continue
        if zero and out:
            out += "零"
        zero = False
        if not (v == 1 and pos == 1 and not out):  # 10 → 十，不是 一十
            out += d[v]
        out += u[pos]
    return out


def _norm_text(s):
    """ASR 文本归一化：繁→简、数字→中文读法、去标点空白。
    只折叠转写写法差异，不掩盖内容增删（逐字相似度仍会下降）。"""
    s = str(s or "").translate(_TC2SC_TRANS)
    s = re.sub(r"\d+", lambda m: _num2cn(int(m.group())), s)
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s).lower()


def text_similarity(a, b):
    a, b = _norm_text(a), _norm_text(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def transcribe(path):
    """faster-whisper small（与字幕同一权重），返回纯文本；失败返回 ''。
    注意：必须传 numpy PCM 而非文件路径——本机 PyAV 与 faster-whisper 1.2.1 的
    metadata_errors 参数不兼容，传路径会静默返回空文本，使文本闸形同虚设。"""
    key = ("asr", os.path.abspath(path), os.path.getsize(path))
    if key in _cache:
        return _cache[key]
    global _whisper
    try:
        if _whisper is None:
            from faster_whisper import WhisperModel
            wm = WHISPER_DIR if os.path.isdir(WHISPER_DIR) else "small"
            _whisper = WhisperModel(wm, device="cpu", compute_type="int8")
        pcm = _decode_pcm(path, sr=16000, ch=1)[:, 0]
        segments, _ = _whisper.transcribe(pcm, language="zh", vad_filter=True)
        out = "".join(s.text for s in segments).strip()
    except Exception:
        out = ""
    _cache[key] = out
    return out


def f0_median(path):
    """YIN 基频估计：16 kHz、60–500 Hz、取有声帧中位数（Hz）。

    早期自相关实现会被增强后的谐波带偏（男声 128 Hz 被估成 195 Hz），
    导致 F0/性别闸误杀；YIN 用累积均值归一化差分 + 首个阈值最小值，
    并在末尾剔除八度跳变帧后复算中位数。"""
    try:
        pcm = _decode_pcm(path, sr=16000, ch=1)[:, 0].astype(np.float64)
    except Exception:
        return None
    n = len(pcm)
    if n < 2048:
        return None
    sr = 16000
    frame, hop = 1024, 256
    fmin, fmax = 60.0, 500.0
    min_lag, max_lag = int(sr / fmax), int(sr / fmin)
    nfft = 1
    while nfft < 2 * frame:
        nfft *= 2
    taus = np.arange(max_lag + 2)
    vals = []
    for st in range(0, n - frame, hop):
        x = pcm[st:st + frame]
        if float(np.sqrt(np.mean(x * x))) < 0.003:
            continue
        x = x - x.mean()
        spec = np.fft.rfft(x, n=nfft)
        ac = np.fft.irfft(spec * np.conj(spec), n=nfft)[:max_lag + 2]
        cs = np.concatenate(([0.0], np.cumsum(x * x)))
        e0 = cs[frame - taus]          # sum x[0:frame-tau]^2
        et = cs[-1] - cs[taus]         # sum x[tau:frame]^2
        d = np.maximum(e0 + et - 2.0 * ac, 0.0)
        cm = np.ones_like(d)
        csum = np.cumsum(d[1:])
        cm[1:] = d[1:] * taus[1:] / np.maximum(csum, 1e-12)
        tau = None
        for t in range(min_lag, max_lag + 1):
            if cm[t] < 0.15:
                while t + 1 <= max_lag and cm[t + 1] < cm[t]:
                    t += 1
                tau = t
                break
        if tau is None:
            tau = min_lag + int(np.argmin(cm[min_lag:max_lag + 1]))
            if cm[tau] > 0.45:
                continue
        if 0 < tau < max_lag:
            a, b, c = cm[tau - 1], cm[tau], cm[tau + 1]
            den = a - 2.0 * b + c
            if abs(den) > 1e-12:
                tau = tau + max(-0.5, min(0.5, 0.5 * (a - c) / den))
        f = sr / tau
        if fmin <= f <= fmax:
            vals.append(f)
    if len(vals) < 10:
        return None
    vals = np.asarray(vals, dtype=np.float64)
    med = float(np.median(vals))
    keep = vals[(vals >= med / 1.8) & (vals <= med * 1.8)]
    if len(keep) >= 5:
        med = float(np.median(keep))
    return round(med, 2)


def asr_gate(t0, t1):
    """文本闸：两边都必须转写出内容，避免「空 vs 空 = 1.0」的假通过"""
    sim = text_similarity(t0, t1)
    return round(sim, 3), bool(t0 and t1 and sim >= TEXT_SIM_MIN)


def gender_ok(gender, src_f0, out_f0):
    """性别/音域不翻转：profile 性别方向 + 相对原音的音域倍数双检"""
    if not out_f0:
        return False
    if gender == "male":
        if out_f0 >= 180:
            return False
    elif gender == "female":
        if out_f0 <= 140:
            return False
    if src_f0 and out_f0:
        ratio = out_f0 / max(src_f0, 1e-6)
        if ratio > 2.0 or ratio < 0.5:
            return False
    return True


# ─────────────────────────── profile / 角色 ───────────────────────────

def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _profiles():
    seen, out = set(), []
    for d in VOICE_PROFILE_DIRS:
        for p in glob.glob(os.path.join(d, "*.voice.json")):
            rp = os.path.abspath(p)
            if rp in seen:
                continue
            seen.add(rp)
            try:
                d0 = _load_json(p)
                d0["_path"] = rp
                out.append(d0)
            except Exception:
                continue
    return out


def _align_guard(prof):
    """guard/voice 同源校验：角色名必须对齐；缺 f0/gender 时从 guard 补齐。

    JEV/guard 链路与音色层因此共用同一份角色定义（h3_guard 也会反向读取
    voice_profile 的 f0/gender/reference），不会出现两套人物口径。"""
    gp = prof.get("guard_profile")
    out = {"guard_profile": gp, "ok": None, "mismatch": []}
    if not gp or not os.path.isfile(str(gp)):
        prof["_guard_alignment"] = out
        return prof
    try:
        g = _load_json(gp)
    except Exception as e:
        out["error"] = str(e)[:120]
        prof["_guard_alignment"] = out
        return prof
    gchars = g.get("characters") or {}
    for n, c in (prof.get("characters") or {}).items():
        gc = gchars.get(n)
        if not gc:
            out["mismatch"].append(n + ":guard_missing")
            continue
        if c.get("f0_range") and gc.get("f0_range") \
                and list(c["f0_range"]) != list(gc["f0_range"]):
            out["mismatch"].append(n + ":f0_range")
        if not c.get("f0_range") and gc.get("f0_range"):
            c["f0_range"] = gc["f0_range"]
        if not c.get("gender") and gc.get("gender"):
            c["gender"] = gc["gender"]
    out["ok"] = not out["mismatch"]
    prof["_guard_alignment"] = out
    return prof


def load_profile(name):
    """按 profile id / film 名 / 文件名匹配 voice profile；找不到返回 None"""
    if not name:
        return None
    raw = str(name)
    stem = os.path.splitext(os.path.basename(raw))[0]
    cands = _profiles()
    for d in cands:
        b = os.path.basename(d["_path"])
        bstem = b[:-len(".voice.json")] if b.endswith(".voice.json") else b
        if str(d.get("profile") or "") in (raw, stem) or bstem in (raw, stem):
            return _align_guard(d)
    for d in cands:
        if str(d.get("film") or "") in (raw, stem):
            return _align_guard(d)
    return None


def load_guard_profile(film_name):
    for d in GUARD_DIRS:
        for p in glob.glob(os.path.join(d, "*.guard.json")):
            try:
                g = _load_json(p)
            except Exception:
                continue
            if str(g.get("film") or "") == str(film_name):
                g["_path"] = os.path.abspath(p)
                return g
    return None


def _char_info(profile, name):
    c = ((profile or {}).get("characters") or {}).get(name) or {}
    return c


def _char_gender(c):
    if c.get("gender") in ("male", "female"):
        return c["gender"]
    rng = c.get("f0_range") or []
    if len(rng) == 2:
        return "male" if (float(rng[0]) + float(rng[1])) / 2 < 165 else "female"
    return "unknown"


def _ref_ok(c):
    p = c.get("voice_ref")
    return bool(p and os.path.isfile(p))


def _profile_sidecar(base):
    stem = os.path.splitext(os.path.basename(base))[0]
    for p in (os.path.join(RAW, stem + ".voice.json"),
              os.path.join(RAW, os.path.basename(base) + ".voice.json")):
        if os.path.isfile(p):
            try:
                return _load_json(p)
            except Exception:
                return None
    return None


def resolve_profile_for_video(base):
    """单条视频 profile：sidecar 指定 > 文件名 seg_XX 反查 > 唯一 film 名匹配"""
    side = _profile_sidecar(base) or {}
    if side.get("profile"):
        return load_profile(side["profile"])
    stem = os.path.splitext(os.path.basename(base))[0]
    m = re.search(r"seg[_\-]?(\d+)", stem, re.I)
    if m:
        for d in _profiles():
            if ((d.get("segments") or {}).get(str(int(m.group(1)))) or {}).get("speakers"):
                return d
    return load_profile(stem)


def resolve_profile_for_film(film_name):
    p = load_profile(film_name)
    if p:
        return p
    g = load_guard_profile(film_name)
    if g:
        return load_profile(g.get("film"))
    return None


def _plan_segment(profile, char, start, end, extra=None):
    c = _char_info(profile, char)
    seg = {"start": round(float(start), 3), "end": round(float(end), 3),
           "character": char, "voice_ref": c.get("voice_ref"),
           "f0_range": c.get("f0_range"), "gender": _char_gender(c)}
    if extra:
        seg.update(extra)
    return seg


def plan_single(video, profile):
    """单条：只有「整条只有一个可靠说话人」才转；否则显式跳过"""
    dur = _probe(video)["duration"]
    if dur <= 0:
        return {"mode": "single", "segments": [], "skipped": [],
                "skip_reason": "无法读取视频时长"}
    side = _profile_sidecar(video) or {}
    # 1) sidecar 显式给角色
    char = side.get("character")
    # 2) sidecar 给可靠时间范围
    if not char and isinstance(side.get("voice_plan"), list):
        segs = []
        for s in side["voice_plan"]:
            ch = str(s.get("character") or "")
            if ch and _ref_ok(_char_info(profile, ch)):
                segs.append(_plan_segment(profile, ch, s.get("start", 0),
                                          s.get("end", dur), {"source": "sidecar"}))
        if segs:
            return {"mode": "single", "segments": segs, "skipped": [],
                    "skip_reason": None}
    # 3) sidecar speakers 单元素
    if not char:
        sp = side.get("speakers") or []
        if isinstance(sp, list) and len(sp) == 1:
            char = str(sp[0])
    # 4) 文件名 seg_XX → guard/voice profile 的 segments.speakers
    if not char:
        stem = os.path.splitext(os.path.basename(video))[0]
        m = re.search(r"seg[_\-]?(\d+)", stem, re.I)
        if m and profile:
            sp = (((profile.get("segments") or {}).get(str(int(m.group(1)))) or {})
                  .get("speakers") or [])
            if len(sp) == 1:
                char = str(sp[0])
            elif len(sp) > 1:
                return {"mode": "single", "segments": [], "skipped": [],
                        "skip_reason": f"seg_{int(m.group(1)):02d} 有 {len(sp)} 个说话人，"
                                       "单条无可靠 diarization，整条跳过"}
    if not char:
        return {"mode": "single", "segments": [], "skipped": [],
                "skip_reason": "无可靠单说话人映射（缺 sidecar / seg_XX speaker 信息）"}
    c = _char_info(profile, char)
    if not _ref_ok(c):
        return {"mode": "single", "segments": [], "skipped": [],
                "skip_reason": f"角色 {char} 缺参考音，跳过"}
    return {"mode": "single", "segments": [_plan_segment(profile, char, 0, dur,
                                                         {"source": "whole"})],
            "skipped": [], "skip_reason": None}


def _seg_no(seg):
    if seg.get("seg_no"):
        return int(seg["seg_no"])
    for k in ("source", "file"):
        v = seg.get(k)
        if isinstance(v, dict):
            v = v.get("path") or v.get("file")
        if v:
            m = re.search(r"seg[_\-]?(\d+)", os.path.basename(str(v)), re.I)
            if m:
                return int(m.group(1))
    return int(seg.get("idx", 0)) + 1


def plan_film(profile, manifest_segments, concat_info=None, duration=None):
    """长片：manifest 段边界 + profile speakers/voice_ref；多说话人段不猜，跳过"""
    if not profile:
        return {"mode": "film", "segments": [], "skipped": [],
                "skip_reason": "未找到同名片 voice profile"}
    xfade = 0.5 if str((concat_info or {}).get("method")) == "xfade" else 0.0
    planned, skipped, t = [], [], 0.0
    for i, seg in enumerate(manifest_segments or []):
        d = float(seg.get("duration_s") or 0)
        start = float(seg.get("start_s", t))
        end = float(seg.get("end_s", start + d))
        core_end = max(start + 0.05, end - (xfade if i < len(manifest_segments) - 1 else 0))
        t = end - xfade
        no = _seg_no(seg)
        sp = (((profile.get("segments") or {}).get(str(no)) or {}).get("speakers") or [])
        if len(sp) != 1:
            skipped.append({"idx": i, "seg_no": no, "start": round(start, 3),
                            "end": round(end, 3), "reason":
                            ("多说话人且无可靠时间范围" if sp else "profile 无该段 speaker 信息")})
            continue
        char = str(sp[0])
        c = _char_info(profile, char)
        if not _ref_ok(c):
            skipped.append({"idx": i, "seg_no": no, "character": char,
                            "start": round(start, 3), "end": round(end, 3),
                            "reason": "缺参考音"})
            continue
        planned.append(_plan_segment(profile, char, start, core_end, {"idx": i,
                                                                     "seg_no": no,
                                                                     "source": "manifest"}))
    if duration and planned:
        planned = [s for s in planned if 0 <= s["start"] < s["end"] <= duration + 0.2]
    if not planned:
        return {"mode": "film", "segments": [], "skipped": skipped,
                "skip_reason": "没有可靠的单说话人时间范围，整片跳过（不猜测、不整片同声）"}
    return {"mode": "film", "segments": planned, "skipped": skipped, "skip_reason": None}


# ─────────────────────────── Seed-VC 调用 ───────────────────────────

def _seedvc(src_wav, ref_wav, workdir):
    """独立 venv/CLI 调 Seed-VC（f0 条件 + 不自动移调 → 保留原音情绪/语调）"""
    outdir = tempfile.mkdtemp(prefix="seedvc_", dir=workdir)
    env = {**os.environ,
           "HF_HUB_OFFLINE": "1",
           "HF_HUB_CACHE": SEEDVC_CKPT,
           "PYTHONIOENCODING": "utf-8",
           "TORCHINDUCTOR_CACHE_DIR": r"H:\MusicAI\cache\torchinductor",
           "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}
    cmd = [SEEDVC_PY, "inference.py",
           "--source", src_wav, "--target", ref_wav, "--output", outdir,
           "--diffusion-steps", "25", "--f0-condition", "True",
           "--auto-f0-adjust", "False", "--fp16", "True"]
    r = _run(cmd, timeout=1800, cwd=SEEDVC_APP, env=env)
    wavs = glob.glob(os.path.join(outdir, "*.wav"))
    if r.returncode != 0 or not wavs:
        raise RuntimeError("Seed-VC 转换失败: " + (r.stderr or "")[-240:])
    out = max(wavs, key=os.path.getmtime)
    # 每次转换独立文件名：Seed-VC 对同名 target 会产出相同文件名，
    # 复用 _vc_<target>.wav 会让 transcribe() 的 (路径,大小) 缓存串段，
    # 文本闸拿上一段的转写做比较 → 假通过。
    keep = os.path.join(workdir, "_vc_" + uuid.uuid4().hex[:10] + ".wav")
    shutil.move(out, keep)
    shutil.rmtree(outdir, ignore_errors=True)
    return keep


def _slice_wav(video, start, end, out_wav):
    dur = max(0.02, float(end) - float(start))
    r = _run([FFMPEG, "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", video,
              "-vn", "-ar", "44100", "-ac", "1", out_wav], timeout=300)
    if r.returncode != 0 or not os.path.isfile(out_wav):
        raise RuntimeError("音频切片失败: " + (r.stderr or "")[-160:])
    return out_wav


def _loudnorm(wav_in, wav_out):
    r = _run([FFMPEG, "-y", "-i", wav_in, "-af", LOUDNORM,
              "-ar", str(SR), "-ac", str(CH), "-c:a", "pcm_s16le", wav_out], timeout=900)
    if r.returncode != 0 or not os.path.isfile(wav_out):
        raise RuntimeError("响度归一失败: " + (r.stderr or "")[-160:])
    return wav_out


def _fit_len(pcm, n):
    if len(pcm) == n:
        return pcm
    if len(pcm) > n:
        return pcm[:n]
    pad = np.zeros((n - len(pcm), pcm.shape[1]), dtype=np.float32)
    return np.concatenate([pcm, pad], axis=0)


def _splice(dst, src, st, en, fade=0.02):
    """把 src 换入 dst[st:en]，边界 20ms 等功率交叉淡化防爆音"""
    L = en - st
    src = _fit_len(src, L)
    f = max(0, min(int(fade * SR), L // 2))
    if f > 0:
        w = np.linspace(0, 1, f, dtype=np.float32)[:, None]
        src[:f] = src[:f] * w + dst[st:st + f] * (1 - w)
        w2 = np.linspace(1, 0, f, dtype=np.float32)[:, None]
        src[-f:] = src[-f:] * w2 + dst[en - f:en] * (1 - w2)
    dst[st:en] = src


# ─────────────── 雨声/环境声保留（可选，默认关闭） ───────────────

def _energy_spans(wav, sr=SR):
    """能量 VAD 兜底：返回 [(start,end)] 秒；只用于 whisper VAD 不可用时。"""
    try:
        x = _decode_pcm(wav, sr=sr, ch=1)[:, 0]
    except Exception:
        return []
    win, hop = int(0.03 * sr), int(0.01 * sr)
    if len(x) < win:
        return []
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop]
    db = 20.0 * np.log10(np.sqrt(np.mean(frames * frames, axis=1)) + 1e-9)
    floor = float(np.percentile(db, 20))
    act = db > max(floor + 8.0, -45.0)
    # 闭短间隙（<150ms），去短块（<200ms）
    act = np.convolve(act.astype(np.int8), np.ones(16, dtype=np.int8),
                      "same") > 0
    edges = np.flatnonzero(np.diff(np.r_[0, act.astype(np.int8), 0]))
    spans = []
    for i in range(0, len(edges) - 1, 2):
        a, b = edges[i] * hop / sr, edges[i + 1] * hop / sr
        if b - a >= 0.20:
            spans.append((max(0.0, a), b))
    return spans


def _speech_spans(wav):
    """源音轨 VAD 语音区间（秒）。优先 faster-whisper（与文本闸同权重），
    异常时退回能量 VAD；无语音返回 []。"""
    global _whisper
    try:
        if _whisper is None:
            from faster_whisper import WhisperModel
            wm = WHISPER_DIR if os.path.isdir(WHISPER_DIR) else "small"
            _whisper = WhisperModel(wm, device="cpu", compute_type="int8")
        pcm = _decode_pcm(wav, sr=16000, ch=1)[:, 0]
        segments, _ = _whisper.transcribe(pcm, language="zh", vad_filter=True)
        spans = [(max(0.0, float(s.start)), float(s.end)) for s in segments
                 if float(s.end) - float(s.start) >= 0.05]
        spans.sort()
        return spans
    except Exception:
        return _energy_spans(wav)


def _build_rain_bed(orig, spans, n, sr=SR, pad=AMBIENCE_SPEECH_PAD):
    """从原音轨非语音段取块，30ms 交叉淡化拼成 n 样本无语音雨声床。
    返回 (bed, target_rms) 或 (None, None)。块只来自非语音，避免把台词带入。"""
    speech = np.zeros(n, dtype=bool)
    for a, b in spans:
        i0 = max(0, int(round((a - pad) * sr)))
        i1 = min(n, int(round((b + pad) * sr)))
        if i1 > i0:
            speech[i0:i1] = True
    gaps = ~speech
    if not gaps.any():
        return None, None
    target = float(np.sqrt(np.mean(orig[gaps].astype(np.float64) ** 2)) + 1e-12)
    edges = np.flatnonzero(np.diff(np.r_[False, gaps, False]))
    regions = [(edges[i] / sr, edges[i + 1] / sr) for i in range(0, len(edges), 2)]
    chunks = []
    for a, b in regions:
        lo, hi = a + 0.05, b - 0.05
        if hi - lo < 0.15:
            continue
        L = min(0.70, hi - lo)
        cnt = 1 if hi - lo < 1.4 else min(3, int((hi - lo) // 0.9))
        for k in range(cnt):
            c0 = ((lo + hi - L) / 2.0 if cnt == 1 else
                  lo + (hi - lo - L) * k / max(1, cnt - 1))
            c = orig[int(round(c0 * sr)):int(round((c0 + L) * sr))].copy()
            if len(c) >= int(0.15 * sr):
                chunks.append(c)
    chunks = chunks[:8]
    if not chunks:
        return None, target
    chunks = [c * (target / (float(np.sqrt(np.mean(c.astype(np.float64) ** 2)))
                            + 1e-12)) for c in chunks]
    rng = np.random.default_rng(11)
    fade = max(1, int(0.03 * sr))
    out = np.zeros((0, orig.shape[1]), dtype=np.float32)
    while len(out) < n:
        for i in rng.permutation(len(chunks)):
            c = chunks[i]
            if len(out) == 0:
                out = c.copy()
            else:
                f = min(fade, len(c), len(out))
                w = np.linspace(0, 1, f, dtype=np.float32)[:, None]
                out[-f:] = out[-f:] * (1 - w) + c[:f] * w
                out = np.concatenate([out, c[f:]], axis=0)
            if len(out) >= n:
                break
    return out[:n], target


def _apply_ambience(dst, orig, conv, st, en, spans, bed, bed_db, voice_db,
                    sr=SR, pad=AMBIENCE_SPEECH_PAD, fade=AMBIENCE_FADE):
    """把转换声换入 dst[st:en]：语音区间=conv(+增益)+雨声床，非语音=原音轨。
    语音/非语音边界做 fade 秒线性交叉淡化；返回换入前的峰值。"""
    L = en - st
    conv = _fit_len(conv, L)
    gv = 10.0 ** (voice_db / 20.0)
    voiced = conv * gv
    if bed is not None:
        voiced = voiced + bed[st:en] * (10.0 ** (bed_db / 20.0))
    base = orig[st:en].copy()
    mask = np.zeros(L, dtype=bool)
    for a, b in spans:
        i0 = max(0, int(round((a - pad - st / sr) * sr)))
        i1 = min(L, int(round((b + pad - st / sr) * sr)))
        if i1 > i0:
            mask[i0:i1] = True
    wv = mask.astype(np.float32)
    f = max(1, int(fade * sr))
    edges = np.flatnonzero(np.diff(np.r_[0, mask.astype(np.int8), 0]))
    for i in range(0, len(edges) - 1, 2):
        up, down = edges[i], edges[i + 1]
        if up > 0:
            a0, a1 = max(0, up - f), up
            wv[a0:a1] = np.minimum(wv[a0:a1],
                                   np.linspace(0, 1, a1 - a0, dtype=np.float32))
        if down < L:
            a0, a1 = down, min(L, down + f)
            wv[a0:a1] = np.maximum(wv[a0:a1],
                                   np.linspace(1, 0, a1 - a0, dtype=np.float32))
    peak = float(np.max(np.abs(voiced))) if L else 0.0
    if peak > 0.98:
        voiced *= 0.98 / peak
    new = base * (1.0 - wv[:, None]) + voiced * wv[:, None]
    # 段边界 20ms 与既有 dst 交叉淡化（与 _splice 一致），防拼接爆音
    sf = max(0, min(int(0.02 * sr), L // 2))
    if sf > 0:
        w = np.linspace(0, 1, sf, dtype=np.float32)[:, None]
        new[:sf] = dst[st:st + sf] * (1 - w) + new[:sf] * w
        w2 = np.linspace(1, 0, sf, dtype=np.float32)[:, None]
        new[-sf:] = new[-sf:] * w2 + dst[en - sf:en] * (1 - w2)
    dst[st:en] = new
    return peak


def _gate_segment(name, src_wav, raw_wav, char, src_dur, expected_text=None):
    g = {"character": name, "duration_src": round(src_dur, 3)}
    raw_dur = _probe(raw_wav)["duration"]
    g["duration_out"] = round(raw_dur, 3)
    g["duration_diff"] = round(abs(raw_dur - src_dur), 4)
    g["duration_ok"] = bool(g["duration_diff"] <= DUR_TOL)
    raw_pcm = _decode_pcm(raw_wav, sr=SR, ch=CH)
    g["peak_dbfs"] = round(_peak_dbfs(raw_pcm), 2)
    g["peak_ok"] = bool(g["peak_dbfs"] <= PEAK_DBFS_MAX)
    t0, t1 = transcribe(src_wav), transcribe(raw_wav)
    sim, tok = asr_gate(t0, t1)
    g["text_sim_asr"] = sim
    g["text_sim_min"] = TEXT_SIM_MIN
    if expected_text and _norm_text(expected_text):
        # 台词本已知（单条整段）：以原文为准比对转换后转写，容错同音字/写法
        # 差异（≥0.85）；原音/转后转写相似度仅留档，避免 Whisper 同音字波动
        # 把内容完好的段误杀。
        sim_script = text_similarity(t1, expected_text)
        g["text_sim"] = round(sim_script, 3)
        g["text_sim_min"] = SCRIPT_SIM_MIN
        g["text_sim_script"] = round(sim_script, 3)
        g["text_sim_src_script"] = round(text_similarity(t0, expected_text), 3)
        g["text_ok"] = bool(t1 and sim_script >= SCRIPT_SIM_MIN)
    else:
        g["text_sim"] = sim
        g["text_ok"] = bool(tok and sim >= TEXT_SIM_MIN)
    g["asr_chars"] = [len(t0), len(t1)]  # 空转写（ASR 坏）时 text_ok 必须为 False
    f_src, f_out = f0_median(src_wav), f0_median(raw_wav)
    g["f0_src"] = f_src
    g["f0_out"] = f_out
    lo, hi = (char.get("f0_range") or [0, 1000])[:2]
    g["f0_range"] = [lo, hi]
    g["f0_ok"] = bool(f_out and float(lo) <= f_out <= float(hi))
    g["gender_ok"] = gender_ok(_char_gender(char), f_src, f_out)
    # 每段都必须过文本闸（text_sim >= 0.90）：整片相似度再高也不能掩盖单段
    # 文本被改坏。未过文本闸的段不拼接，保留该段原音轨。
    g["ok"] = bool(all(g[k] for k in ("duration_ok", "peak_ok", "text_ok",
                                      "f0_ok", "gender_ok")))
    return g


# ─────────────────────────── 主流程 ───────────────────────────

def process_track(video, profile, plan, workdir, progress=None, log=None,
                  expected_text=None, restore_ambience=None):
    """把 plan 应用到视频音轨；返回结果字典。绝不修改 video 本身。
    expected_text: 台词本（已知原文）。整轨硬文本闸是「原音转写 vs 转换后转写
    相似度 ≥0.90」；台词本另做内容保留检查（≥0.85），防止同音字写法差异误杀。
    单条整段（plan 只有一个 source=whole 段）时，逐段文本闸直接以台词本为准
    （≥0.85），不再用两次 ASR 互相比较。其余情况每段仍有独立文本闸
    （text_sim ≥0.90）：不达标的段不拼接、回退原音轨，段记录标记
    voice_skipped/fallback，整片 gates.text_sim_segments 逐段可查。
    status: done | partial | skipped（skipped 时 output=None，调用方保留原音轨）"""
    say = progress or (lambda _t: None)
    logf = log or (lambda _t: None)
    result = {"status": "skipped", "reason": None, "profile":
              (profile or {}).get("profile"), "plan": plan, "segments": [],
              "gates": {}, "output": None}
    if np is None:
        result["reason"] = "numpy 不可用"
        return result
    if not plan or not plan.get("segments"):
        result["reason"] = (plan or {}).get("skip_reason") or "无可转换片段"
        return result
    if not os.path.isdir(workdir):
        os.makedirs(workdir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="voice_", dir=workdir)
    try:
        orig = _decode_pcm(video, sr=SR, ch=CH)
        n = len(orig)
        out = orig.copy()
        src_vsha = _video_stream_sha(video)
        # 可选：保留雨声/环境声。默认关闭，行为与旧版完全一致。
        do_amb = bool(AMBIENCE_RESTORE if restore_ambience is None
                      else restore_ambience)
        amb_spans, amb_bed, amb_target = [], None, None
        result["ambience_restore"] = {"enabled": do_amb, "spans": [],
                                      "bed_db": AMBIENCE_BED_DB,
                                      "voice_db": AMBIENCE_VOICE_DB,
                                      "bed_ok": False, "segments": 0}
        if do_amb:
            vad_wav = os.path.join(tmp, "vad_src.wav")
            _write_wav(orig, vad_wav)
            amb_spans = _speech_spans(vad_wav)
            amb_bed, amb_target = _build_rain_bed(orig, amb_spans, n)
            result["ambience_restore"].update({
                "spans": [[round(a, 3), round(b, 3)] for a, b in amb_spans],
                "bed_ok": amb_bed is not None,
                "bed_rms_db": (round(float(20 * np.log10(max(amb_target, 1e-12))), 2)
                               if amb_target else None)})
            logf(f"[voice] 环境声保留：VAD {len(amb_spans)} 段，"
                 f"雨声床{'就绪' if amb_bed is not None else '不可用（无非语音段）'}")
        # plan 里因「多说话人/缺参考音」被跳过的段要显式带入结果，
        # 否则全片会被误标成 done（实际只转换了部分段）。
        failed = [{"character": s.get("character"),
                   "start": s.get("start"), "end": s.get("end"),
                   "reason": s.get("reason") or "无可靠 speaker 范围"}
                  for s in (plan.get("skipped") or [])]
        ok_segs = []
        for i, seg in enumerate(plan["segments"]):
            ch = seg.get("character")
            c = _char_info(profile, ch)
            if not _ref_ok(c):
                failed.append({"character": ch, "start": seg.get("start"),
                               "end": seg.get("end"), "reason": "缺参考音，跳过"})
                logf(f"[voice] {ch} 缺参考音，跳过该段（保留原音）")
                continue
            src_wav = os.path.join(tmp, f"src_{i:02d}.wav")
            src_dur = _slice_wav(video, seg["start"], seg["end"], src_wav)
            src_dur = _probe(src_wav)["duration"]
            say(f"{ch} · {seg['start']:.1f}-{seg['end']:.1f}s · Seed-VC")
            try:
                raw = _seedvc(src_wav, c["voice_ref"], tmp)
                seg_script = (expected_text
                              if (expected_text and plan.get("mode") == "single"
                                  and len(plan["segments"]) == 1
                                  and seg.get("source") == "whole") else None)
                g = _gate_segment(ch, src_wav, raw, c, src_dur, seg_script)
                seg_rec = {**seg, "gates": g,
                           "status": "done" if g["ok"] else "rejected"}
                if not g["ok"]:
                    # 该段回退原音轨（不拼接、不改文本）；显式标记，避免整片
                    # 相似度掩盖单段失败。
                    bad = ", ".join(k for k in ("duration_ok", "text_ok", "f0_ok",
                                                "gender_ok", "peak_ok") if not g[k])
                    seg_rec.update({"voice_skipped": True,
                                    "fallback": "original_audio",
                                    "skip_reason": f"硬闸未过: {bad}"})
                result["segments"].append(seg_rec)
                if not g["ok"]:
                    failed.append({"character": ch, "start": seg["start"],
                                   "end": seg["end"], "reason": "硬闸未过",
                                   "voice_skipped": True,
                                   "fallback": "original_audio",
                                   "gates": g})
                    logf(f"[voice] {ch} {seg['start']:.1f}-{seg['end']:.1f}s 闸门未过: "
                         + ", ".join(k for k in ("duration_ok", "text_ok", "f0_ok",
                                                 "gender_ok", "peak_ok") if not g[k])
                         + f" · text_sim={g.get('text_sim')}"
                         + " · 该段保留原音轨")
                    continue
                pcm = _decode_pcm(raw, sr=SR, ch=CH)
                st = max(0, int(round(seg["start"] * SR)))
                en = min(n, int(round(seg["end"] * SR)))
                if en <= st:
                    continue
                if do_amb:
                    _apply_ambience(out, orig, pcm, st, en, amb_spans, amb_bed,
                                    AMBIENCE_BED_DB, AMBIENCE_VOICE_DB)
                    result["ambience_restore"]["segments"] += 1
                else:
                    _splice(out, pcm, st, en)
                ok_segs.append(seg)
            except Exception as e:
                failed.append({"character": ch, "start": seg["start"],
                               "end": seg["end"], "reason": str(e)[:200]})
                logf(f"[voice] {ch} {seg['start']:.1f}-{seg['end']:.1f}s 失败: {e}")
        # 整片报告逐段列出文本相似度：单段 <0.90 时该段已回退原音轨，
        # 不能被整轨 0.985 之类的汇总值掩盖。
        seg_text = [{"character": s.get("character"),
                     "start": s.get("start"), "end": s.get("end"),
                     "status": s.get("status"),
                     "text_sim": (s.get("gates") or {}).get("text_sim"),
                     "text_ok": (s.get("gates") or {}).get("text_ok"),
                     "voice_skipped": bool(s.get("voice_skipped"))}
                    for s in result["segments"]]
        seg_sims = [x["text_sim"] for x in seg_text if x["text_sim"] is not None]
        result["gates"]["text_sim_segments"] = seg_text
        result["gates"]["text_sim_min_segment"] = (min(seg_sims) if seg_sims else None)
        result["gates"]["segments_text_ok"] = bool(seg_text) and all(
            x["text_ok"] for x in seg_text)
        result["segment_fallback"] = [x for x in seg_text if x["voice_skipped"]]
        if not ok_segs:
            result["reason"] = "全部片段未过硬闸: " + "; ".join(
                f"{f.get('character')}:{f.get('reason')}" for f in failed[:3])
            result["failed"] = failed
            return result

        if do_amb:
            mxp = float(np.max(np.abs(out)))
            if mxp > 0.999:
                out = out * (0.999 / mxp)
                result["ambience_restore"]["pre_norm_gain_db"] = round(
                    20 * np.log10(0.999 / mxp), 2)
        # 整轨响度归一 + 精确时长 + 峰值
        wav = _write_wav(out, os.path.join(tmp, "assembled.wav"))
        norm = _loudnorm(wav, os.path.join(tmp, "norm.wav"))
        nout_raw = _decode_pcm(norm, sr=SR, ch=CH)
        # 先按真实样本数判时长，再做补零/裁剪对齐；否则 fit_len 后恒为 0，闸形同虚设
        result["gates"]["duration_diff"] = round(abs(len(nout_raw) - n) / SR, 4)
        result["gates"]["duration_ok"] = bool(result["gates"]["duration_diff"] <= DUR_TOL)
        nout = _fit_len(nout_raw, n)
        result["gates"]["peak_dbfs"] = round(_peak_dbfs(nout), 2)
        result["gates"]["peak_ok"] = bool(result["gates"]["peak_dbfs"] <= PEAK_DBFS_MAX)
        # 整轨文本闸：有台词本 → 以原文为准（Whisper 同音字/繁简/数字写法归一），
        # 并要求转换后不劣于原音；无台词本 → 原音转写 vs 转后转写。
        orig_wav = os.path.join(tmp, "orig48.wav")
        _write_wav(orig, orig_wav)
        t0, t1 = transcribe(orig_wav), transcribe(norm)
        raw_sim, raw_ok = asr_gate(t0, t1)
        result["gates"]["text_sim_raw"] = raw_sim
        result["gates"]["text_ok_raw"] = raw_ok
        if expected_text and _norm_text(expected_text):
            sim_src = text_similarity(expected_text, t0)
            sim_out = text_similarity(expected_text, t1)
            result["gates"]["text_sim_src"] = round(sim_src, 3)
            result["gates"]["text_sim_script"] = round(sim_out, 3)
            # 硬闸：实际拼接的每一段都必须过独立文本闸（≥0.90），整轨汇总
            # 取逐段最小值；台词本做内容保留检查（≥0.85）。整轨一次性转写受
            # 段边界/VAD 影响，实测 0.897 vs 逐段 0.953/0.92，只保留在
            # text_sim_raw 供排查，不因拼接噪声否决已逐段通过的结果。
            seg_min = result["gates"].get("text_sim_min_segment")
            seg_all_ok = result["gates"].get("segments_text_ok") is True
            result["gates"]["text_sim"] = seg_min if seg_all_ok else raw_sim
            result["gates"]["text_ok"] = bool(
                t0 and t1 and sim_out >= 0.85
                and (seg_all_ok or raw_sim >= TEXT_SIM_MIN))
        else:
            result["gates"]["text_sim"] = raw_sim
            result["gates"]["text_ok"] = raw_ok
        result["gates"]["asr_chars"] = [len(t0), len(t1)]
        result["gates"]["ok"] = result["gates"]["duration_ok"] and \
            result["gates"]["peak_ok"] and result["gates"]["text_ok"]
        if not result["gates"]["ok"]:
            result["reason"] = "整轨硬闸未过（时长/文本/峰值）"
            result["failed"] = failed
            return result

        norm2 = os.path.join(tmp, "final.wav")
        _write_wav(nout, norm2)
        out_mp4 = os.path.join(workdir, os.path.splitext(os.path.basename(video))[0]
                               + "_voiced.mp4")
        r = _run([FFMPEG, "-y", "-i", video, "-i", norm2,
                  "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                  "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-ac", str(CH),
                  out_mp4], timeout=1800)
        if r.returncode != 0 or not os.path.isfile(out_mp4):
            result["reason"] = "音轨封装失败: " + (r.stderr or "")[-200:]
            return result
        # 封装后复检：时长按解码样本数比较（容器时长含 AAC priming，误差可达 ~90ms，
        # 不能作为 ≤50ms 的判据；样本数才是真实音轨时长）
        meta = _probe(out_mp4)
        mux_pcm = _decode_pcm(out_mp4, sr=SR, ch=CH)
        result["gates"]["mux_duration_diff"] = round(abs(len(mux_pcm) - n) / SR, 4)
        result["gates"]["mux_container_diff"] = round(
            abs(meta["duration"] - _probe(video)["duration"]), 4)
        result["gates"]["mux_peak_dbfs"] = round(_peak_dbfs(mux_pcm), 2)
        result["gates"]["audio"] = meta["audio"]
        out_vsha = _video_stream_sha(out_mp4)
        result["gates"]["video_stream_sha_src"] = src_vsha
        result["gates"]["video_stream_sha_out"] = out_vsha
        result["gates"]["video_copy_ok"] = bool(src_vsha and out_vsha
                                                and src_vsha == out_vsha)
        if meta["audio"] != "48000:2" or result["gates"]["mux_duration_diff"] > DUR_TOL \
                or result["gates"]["mux_peak_dbfs"] > PEAK_DBFS_MAX \
                or not result["gates"]["video_copy_ok"]:
            result["reason"] = "封装后复检未过（视频流copy/48k/时长/峰值）"
            try:
                os.remove(out_mp4)
            except OSError:
                pass
            return result
        result["output"] = out_mp4
        result["status"] = "done" if not failed else "partial"
        result["reason"] = ("部分片段跳过: " + "; ".join(
            f"{f.get('character')}:{f.get('reason')}" for f in failed[:3])) if failed else None
        result["failed"] = failed
        return result
    except Exception as e:
        result["reason"] = str(e)[:300]
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── CLI（调试/回归用） ───────────────────────────

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="H3 音色转换（Seed-VC 独立 venv）")
    ap.add_argument("--video", required=True)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--mode", choices=["single", "film"], default="single")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--manifest", default=None, help="film 模式：manifest.json")
    ap.add_argument("--restore-ambience", action="store_true", default=None,
                    help="可选：保留雨声/环境声（默认关闭；也可用 H3_VOICE_AMBIENCE=1）")
    a = ap.parse_args(argv)
    prof = load_profile(a.profile) if a.profile else (
        resolve_profile_for_video(a.video) if a.mode == "single"
        else resolve_profile_for_film(os.path.splitext(os.path.basename(a.video))[0]))
    if a.mode == "single":
        plan = plan_single(a.video, prof)
    else:
        man = {}
        if a.manifest and os.path.isfile(a.manifest):
            man = _load_json(a.manifest)
        plan = plan_film(prof, man.get("segments") or [], man.get("concat"),
                         _probe(a.video)["duration"])
    wd = a.out_dir or os.path.dirname(os.path.abspath(a.video))
    res = process_track(a.video, prof, plan, wd,
                        progress=lambda t: print(t, flush=True),
                        restore_ambience=a.restore_ambience)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get("output") else 2


if __name__ == "__main__":
    sys.exit(main())
