# -*- coding: utf-8 -*-
"""H3 一致性守卫（最小可行版）。

职责分层（不碰成片、不碰模型、不强制重启）：
1. 生成前：build_prompt() 把角色/服装/音色/段间衔接约束注入 H3 提示词；
   prepare_reference_images() 可选把角色定妆图复制到 ComfyUI input 作为首帧候选；
   reference_audio 只记录、不注入（当前 H3 模板没有音频参考输入，见 doctor 输出）。
2. 生成后：check_segments() 对跨段帧做人脸身份、服装色彩、简易基频检查，
   输出逐段 verdict / flags / retry_recommended。
3. 编排层：should_retry() 是唯一的重试判定入口，gen_segments / process_film 可调用。

设计边界（不要夸大）：
- 人脸：YuNet 检测 + SFace 余弦相似度，属于工程启发式，不是生物识别；
  侧脸、逆光、人脸小于约 48px、遮挡时只能报“不可核验”，不能报“一致”。
- 服装：躯干 ROI 的亮度/黑白占比/LAB 均值；只适合“段内突然换装”和
  “同一 costume_id 的跨段漂移”，不识别款式语义。
- 音色：自相关 F0 中位数与性别频带覆盖率；没有参考音频输入，不能保证音色复刻，
  只能提示“本段基频落在角色设定频带之外/覆盖不足”。
- 纯生成随机性、量化损失、模型对长提示词的注意力衰减无法被本模块消除。

CLI:
  python h3_guard.py doctor [--json]
  python h3_guard.py prompt --profile P --segment 7 [--base-file F] [--repair] [--out X]
  python h3_guard.py check --profile P --clips DIR_OR_FILES [--only 07,09] --workdir W --out R.json [--markdown R.md]
"""
import argparse
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import wave

VERSION = "0.1.0"
DEFAULT_MODEL_DIR = r"H:\h3cache\models\h3_guard"
DEFAULT_FFMPEGS = [
    r"D:\ComfyUI\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe",
]
YUNET_NAME = "face_detection_yunet_2023mar.onnx"
SFACE_NAME = "face_recognition_sface_2021dec.onnx"

# 判定阈值全部集中在这里，便于按片校准；默认值来自《同一场雨》只读标定。
TH = {
    "face_min_px": 48,
    "face_frontal": 0.55,
    "face_margin": 0.06,
    "face_ref_high_n": 10,
    "face_ref_high": 0.32,
    "face_ref_low": 0.22,
    "face_ref_med": 0.35,
    "face_late_low": 0.22,
    "costume_dark": 0.18,
    "costume_white": 0.18,
    "costume_L": 18.0,
    "costume_window_L": 25.0,
    "costume_cross_L": 20.0,
    "costume_cross_dark": 0.22,
    "voice_min_voiced": 50,
    "voice_band_min": 0.08,
}

BOUNDARIES = [
    "提示词/参考图/首末帧/重试只能降低漂移概率，不能消除扩散模型随机性。",
    "当前 H3 模板 MiniMaxH3ImageToVideo 只接受 first_frame 图像，没有音频参考输入；"
    "音色只能靠文字描述 + 事后 F0 检查，不能做到参考音频克隆。",
    "人脸检查依赖 YuNet+SFace；侧脸/逆光/小于 48px 的人脸会报“不可核验”，不是通过。",
    "服装检查是躯干 ROI 色彩统计，不识别款式语义；计划内的换装必须在 profile 里改 costume_id。",
    "量化/int8 模型带来的细节损失、长提示词注意力衰减，守卫无法检测或修复。",
]


# ────────────────────────────── 基础工具 ──────────────────────────────
def _safe_name(s):
    return re.sub(r"[^\w.\-]+", "_", str(s), flags=re.UNICODE)[:80]


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dump_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def ffmpeg_path():
    """优先环境变量，其次 ComfyUI venv 内置，最后 PATH。"""
    for p in [os.environ.get("H3_GUARD_FFMPEG")] + DEFAULT_FFMPEGS:
        if p and os.path.isfile(p):
            return p
    return shutil.which("ffmpeg")


def model_dir():
    return os.environ.get("H3_GUARD_MODEL_DIR") or DEFAULT_MODEL_DIR


def _import_cv2():
    try:
        import cv2  # noqa
        import numpy as np  # noqa
        return cv2, np
    except Exception as e:
        raise RuntimeError("H3 守卫需要 opencv-python 与 numpy：%s" % e)


# ────────────────────────────── profile ──────────────────────────────
def load_profile(path):
    """读 profile，校验必填字段，把相对路径解析为 profile 所在目录。"""
    p = os.path.abspath(path)
    prof = _load_json(p)
    base = os.path.dirname(p)
    if not isinstance(prof.get("characters"), dict) or not prof["characters"]:
        raise ValueError("profile 缺 characters")
    if not isinstance(prof.get("segments"), dict):
        raise ValueError("profile 缺 segments")
    for name, c in prof["characters"].items():
        if not isinstance(c, dict):
            raise ValueError("character %s 不是对象" % name)
        for k in ("face_ref", "reference_image"):
            v = c.get(k)
            if v and not os.path.isabs(v):
                c[k] = os.path.normpath(os.path.join(base, v))
    # 音色层与守卫共用同一角色口径：guard 缺 f0/gender/voice_ref 时从 voice profile 补齐
    # （只补不覆盖，避免两套人物定义漂移）
    vp = prof.get("voice_profile")
    if vp:
        if not os.path.isabs(vp):
            vp = os.path.normpath(os.path.join(base, vp))
        if os.path.isfile(vp):
            try:
                vprof = _load_json(vp)
                for name, vc in (vprof.get("characters") or {}).items():
                    c = prof["characters"].get(name)
                    if not isinstance(vc, dict) or not isinstance(c, dict):
                        continue
                    for k in ("f0_range", "gender", "voice_ref"):
                        if c.get(k) in (None, "", []) and vc.get(k) not in (None, "", []):
                            c[k] = vc[k]
                prof["_voice_profile"] = vp
            except Exception:
                pass
    prof["_path"] = p
    prof["_dir"] = base
    return prof


def _seg_conf(prof, seg_idx):
    return (prof.get("segments") or {}).get(str(int(seg_idx))) or {}


def _char_names(seg):
    names = list((seg.get("costume") or {}).keys())
    for n in seg.get("characters") or []:
        if n not in names:
            names.append(n)
    for n in seg.get("speakers") or []:
        if n not in names:
            names.append(n)
    return names


def _costume_desc(prof, costume_id):
    if not costume_id:
        return None
    c = (prof.get("costumes") or {}).get(costume_id)
    if isinstance(c, str):
        return c
    return (c or {}).get("desc") or costume_id


# ────────────────────────────── 生成前 ──────────────────────────────
_CONFLICT_RE = re.compile(r"(外搭|外套|夹克|大衣|羽绒|围巾|换装|换上|脱下|披上)")
_ADDED_LAYER_RE = re.compile(r"(外搭|外套|夹克|大衣|羽绒|围巾|披上)[^，。；\n]{0,24}")


def _prompt_conflicts(base, seg):
    out = []
    if seg.get("forbid_added_layers") and _CONFLICT_RE.search(base or ""):
        out.append({
            "code": "ADDED_LAYER_CONFLICT",
            "severity": "medium",
            "detail": "提示词提到外套/换装，但本段要求延续首帧服装；模型可能在段内加衣。",
        })
    return out


def _repair_prompt(base, seg):
    """只做保守替换：把“外搭/外套”类短语改成“以首帧服装为准，不穿外套”。"""
    if not seg.get("forbid_added_layers"):
        return base, []
    notes = []
    new = _ADDED_LAYER_RE.sub("以首帧服装为准，不穿外套", base or "")
    if new != base:
        notes.append("已把提示词中的外套/换装短语改写为“以首帧服装为准，不穿外套”")
    return new, notes


def build_prompt(base_prompt, prof, seg_idx, repair=False):
    """构造注入一致性约束的提示词。返回 dict(prompt, warnings, notes, characters)。"""
    seg = _seg_conf(prof, seg_idx)
    names = _char_names(seg)
    base = (base_prompt or "").strip()
    if repair and seg.get("forbid_added_layers"):
        base, notes = _repair_prompt(base, seg)
    else:
        notes = []
    lines = ["【一致性守卫·角色与服装锁定】本段是同一部连续短剧的镜头，以下为硬约束："]
    for name in names:
        c = prof["characters"].get(name)
        if not c:
            continue
        cid = (seg.get("costume") or {}).get(name)
        desc = _costume_desc(prof, cid) or "与首帧/上一段相同，不得改变颜色与款式"
        voice = c.get("voice") or ""
        lines.append(
            f"{name}：{c.get('identity', '').strip()}；本段服装={desc}；"
            f"声音={voice}。" if voice else
            f"{name}：{c.get('identity', '').strip()}；本段服装={desc}。"
        )
    lines.append(
        "所有角色在段内不得换脸、换发型、换年龄、换服装；镜头切换前后必须是同一张脸、同一套衣服。"
    )
    if seg.get("forbid_added_layers"):
        lines.append("从首帧到结束只能穿首帧中的服装，禁止在段内新增或脱下外套、围巾、包等外层衣物。")
    if seg.get("speakers"):
        lines.append("本段只允许以下角色开口说话：" + "、".join(seg["speakers"]) + "；其他人不得说话。")
    if seg.get("start_frame"):
        lines.append("本段第一帧必须与给定首帧完全一致，后续动作从首帧自然开始。")
    lines.append("画面不出现任何字幕、文字、水印；只保留环境音，无背景音乐。")
    suffix = (
        "【一致性守卫·收尾】再次确认：角色脸、发型、服装与首帧/上一段一致；"
        "声音与角色设定一致；禁止段内换装、换脸、换人。"
    )
    prompt = "\n".join(lines) + "\n" + base + "\n" + suffix
    warnings = _prompt_conflicts(base_prompt or "", seg)
    return {
        "prompt": prompt,
        "warnings": warnings,
        "notes": notes,
        "characters": names,
        "segment": int(seg_idx),
        "profile": prof.get("_path"),
        "constraints": lines,
    }


def prepare_reference_images(prof, seg_idx, comfy_input=None):
    """把 profile 中角色的定妆图复制成 ComfyUI input 候选；不覆盖已有文件（同名同内容除外）。"""
    out = {}
    for name in _char_names(_seg_conf(prof, seg_idx)):
        c = prof["characters"].get(name) or {}
        src = c.get("reference_image") or c.get("face_ref")
        if not src or not os.path.isfile(src):
            continue
        item = {"source": src, "input_name": None}
        if comfy_input:
            os.makedirs(comfy_input, exist_ok=True)
            dst_name = f"h3guard_{_safe_name(prof.get('film') or 'film')}_{_safe_name(name)}.png"
            dst = os.path.join(comfy_input, dst_name)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
            item["input_name"] = dst_name
        out[name] = item
    return out


def reference_audio_status(prof):
    """当前 H3 模板没有音频参考输入：只回报 profile 是否给了参考音频，不假装支持。"""
    p = prof.get("reference_audio")
    return {
        "path": p if p else None,
        "exists": bool(p and os.path.isfile(p)),
        "supported": False,
        "reason": "MiniMaxH3ImageToVideo 没有音频输入端口；音色只能文字描述 + 事后 F0 检查。",
    }


# ────────────────────────────── 人脸 ──────────────────────────────
def _frontal_score(face):
    import numpy as np
    re, le, nose = np.array(face[4:6]), np.array(face[6:8]), np.array(face[8:10])
    ed = float(np.linalg.norm(le - re))
    fw = max(float(face[2]), 1.0)
    if ed < 0.12 * fw:
        return 0.0
    return float(max(0.0, 1.0 - abs((re[0] + le[0]) / 2 - nose[0]) / (ed + 1e-6)))


class _FaceEngine:
    def __init__(self):
        cv2, np = _import_cv2()
        self.cv2, self.np = cv2, np
        md = model_dir()
        self.yunet = os.path.join(md, YUNET_NAME)
        self.sface = os.path.join(md, SFACE_NAME)
        if not os.path.isfile(self.yunet) or not os.path.isfile(self.sface):
            raise RuntimeError(
                "缺少人脸模型：%s / %s（放到 %s 或设置 H3_GUARD_MODEL_DIR）"
                % (YUNET_NAME, SFACE_NAME, md)
            )
        self.det = cv2.FaceDetectorYN.create(self.yunet, "", (320, 320), 0.55, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(self.sface, "")

    def faces(self, img):
        cv2 = self.cv2
        h, w = img.shape[:2]
        self.det.setInputSize((w, h))
        _, fs = self.det.detect(img)
        out = []
        if fs is None:
            return out
        for f in fs:
            try:
                aligned = self.rec.alignCrop(img, f)
                feat = self.rec.feature(aligned).flatten()
            except Exception:
                continue
            out.append({
                "box": [float(v) for v in f[:4]],
                "feat": feat,
                "size": float(f[2]),
                "frontal": _frontal_score(f),
                "score": float(f[-1]) if len(f) >= 15 else 0.0,
            })
        return out

    def ref_embedding(self, path):
        cv2 = self.cv2
        img = cv2.imread(path)
        if img is None:
            raise RuntimeError("读不到参考图: " + path)
        fs = self.faces(img)
        if not fs:
            raise RuntimeError("参考图里没有检出人脸: " + path)
        return max(fs, key=lambda z: z["size"] * max(z["frontal"], 0.2))["feat"]

    def cos(self, a, b):
        np = self.np
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))

    def centroid(self, feats):
        np = self.np
        if not feats:
            return None
        m = np.mean(feats, axis=0)
        return m / (np.linalg.norm(m) + 1e-9)


def _video_reader(path):
    cv2, _ = _import_cv2()
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None, 0, 0, 0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    return cap, total, w, h


def _read_at(cap, idx):
    cap.set(1, max(0, int(idx)))
    ok, fr = cap.read()
    return fr if ok else None


def _sample_indices(total, n, start=0.03, end=0.97):
    if total <= 0:
        return []
    if n <= 1:
        return [int(total * start)]
    return [min(total - 1, int(total * (start + (end - start) * k / (n - 1)))) for k in range(n)]


def _assign_char(engine, refs, face):
    """返回 (char, sim, margin)；margin 不足返回 (None,...)。"""
    scores = [(name, engine.cos(face["feat"], ref)) for name, ref in refs.items()]
    if not scores:
        return None, 0.0, 0.0
    scores.sort(key=lambda z: -z[1])
    if len(scores) == 1:
        return scores[0][0], scores[0][1], 1.0
    return scores[0][0], scores[0][1], scores[0][1] - scores[1][1]


def _face_analysis(engine, path, prof, seg_idx, total, cap, refs, n=32):
    seg = _seg_conf(prof, seg_idx)
    expected = _char_names(seg)
    names = [n for n in expected if n in refs]
    feats = {n: [] for n in names}
    wins = {n: [[] for _ in range(8)] for n in names}
    detected = 0
    for j, idx in enumerate(_sample_indices(total, n)):
        fr = _read_at(cap, idx)
        if fr is None:
            continue
        for f in engine.faces(fr):
            detected += 1
            if f["size"] < TH["face_min_px"] or f["frontal"] < TH["face_frontal"]:
                continue
            if not names:
                continue
            name, sim, margin = _assign_char(engine, refs, f)
            if name is None or name not in feats or margin < TH["face_margin"]:
                continue
            feats[name].append(f["feat"])
            wins[name][min(7, int(idx * 8 / max(total, 1)))].append(f["feat"])
    out = {}
    for name in names:
        cent = engine.centroid(feats[name])
        ref_sim = engine.cos(cent, refs[name]) if cent is not None else None
        win_rows = []
        for k, arr in enumerate(wins[name]):
            c = engine.centroid(arr)
            win_rows.append({
                "window": k,
                "t0": round(k * 1.25, 2),
                "t1": round((k + 1) * 1.25, 2),
                "n": len(arr),
                "ref_sim": round(engine.cos(c, refs[name]), 3) if c is not None else None,
            })
        out[name] = {
            "n": len(feats[name]),
            "ref_sim": round(ref_sim, 3) if ref_sim is not None else None,
            "windows": win_rows,
        }
    return {"detected_faces": detected, "characters": out}


def _face_flags(face_res, seg_idx, prof):
    seg = _seg_conf(prof, seg_idx)
    flags = []
    for name, r in (face_res.get("characters") or {}).items():
        n, sim = r["n"], r["ref_sim"]
        if n == 0:
            flags.append({
                "code": "FACE_UNVERIFIABLE", "character": name, "severity": "low",
                "detail": "本段没有达到 48px/正脸阈值的可核验人脸；不能判为一致，也不能判为不一致。",
            })
            continue
        if sim is None:
            continue
        if sim < TH["face_ref_low"]:
            flags.append({
                "code": "FACE_REF_LOW", "character": name, "severity": "high",
                "detail": f"人脸质心与定妆参考余弦={sim:.3f} < {TH['face_ref_low']}（n={n}），疑似换脸/脸型漂移。",
            })
        elif n >= TH["face_ref_high_n"] and sim < TH["face_ref_high"]:
            flags.append({
                "code": "FACE_REF_LOW", "character": name, "severity": "high",
                "detail": f"人脸质心与定妆参考余弦={sim:.3f} < {TH['face_ref_high']}（n={n}，样本充足），疑似身份漂移。",
            })
        elif sim < TH["face_ref_med"]:
            flags.append({
                "code": "FACE_REF_BORDERLINE", "character": name, "severity": "medium",
                "detail": f"人脸质心与定妆参考余弦={sim:.3f}（n={n}），低于稳妥线 {TH['face_ref_med']}，建议人工复核。",
            })
        # 末窗低相似：只作咨询，避免把逆光/侧脸误判成换脸
        late = [w for w in r["windows"] if w["n"] >= 3 and w["ref_sim"] is not None]
        if late:
            w = min(late, key=lambda z: z["ref_sim"])
            if w["ref_sim"] < TH["face_late_low"] and sim >= TH["face_ref_med"]:
                flags.append({
                    "code": "FACE_LATE_WINDOW_LOW", "character": name, "severity": "low",
                    "detail": f"{w['t0']}-{w['t1']}s 窗口参考相似度={w['ref_sim']:.3f}（n={w['n']}），末段可能漂移；受光照/角度影响，仅咨询。",
                })
    return flags


# ────────────────────────────── 服装 ──────────────────────────────
def _torso_roi(frame, face):
    h, w = frame.shape[:2]
    x, y, fw, fh = face[:4]
    x0 = int(max(0, x - 0.55 * fw)); x1 = int(min(w, x + 1.55 * fw))
    y0 = int(min(h - 1, y + 0.75 * fh)); y1 = int(min(h, y + 2.3 * fh))
    if x1 - x0 < 20 or y1 - y0 < 20:
        return None
    return frame[y0:y1, x0:x1]


def _color_sig(roi):
    cv2, np = _import_cv2()
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)
    v = hsv[..., 2].astype(np.float32) / 255.0
    s = hsv[..., 1].astype(np.float32) / 255.0
    return {
        "dark": round(float((v < 0.28).mean()), 3),
        "white": round(float(((v > 0.72) & (s < 0.28)).mean()), 3),
        "L": round(float(lab[..., 0].mean()), 1),
        "a": round(float(lab[..., 1].mean()), 1),
        "b": round(float(lab[..., 2].mean()), 1),
        "n_px": int(roi.shape[0] * roi.shape[1]),
    }


def _median_sig(sigs):
    if not sigs:
        return None
    keys = ("dark", "white", "L", "a", "b")
    return {k: round(float(sorted(s[k] for s in sigs)[len(sigs) // 2]), 3) for k in keys}


def _costume_delta(a, b):
    if not a or not b:
        return None
    return {k: round(float(b[k]) - float(a[k]), 3) for k in ("dark", "white", "L", "a", "b")}


def _costume_analysis(engine, path, prof, seg_idx, total, cap, refs, n_windows=8):
    seg = _seg_conf(prof, seg_idx)
    expected = _char_names(seg)
    with_ref = [n for n in expected if n in refs]
    # 多人段里若有人没有参考图，宁可不做服装归属，也不把路人的衣服算到主角头上
    names = with_ref if (len(expected) <= 1 or len(with_ref) == len(expected)) else []
    skipped = [n for n in expected if n not in names]
    wins = {n: [] for n in names}
    first_sigs = {n: [] for n in names}
    first_frames = {n: [] for n in names}

    def _pick_name(faces, names, refs):
        faces = sorted(faces, key=lambda z: -z["size"])
        for f in faces:
            if len(names) == 1:
                return names[0], f
            if refs:
                cand, sim, margin = _assign_char(engine, refs, f)
                if cand in names and margin >= TH["face_margin"]:
                    return cand, f
        return None, None

    # 真正的首帧基线：i2v 的首帧就是输入图，服装跳变应相对它判断
    for idx in range(0, min(6, max(1, total))):
        fr = _read_at(cap, idx)
        if fr is None:
            continue
        faces = [f for f in engine.faces(fr) if f["size"] >= 40 and f["frontal"] >= 0.35]
        if not faces:
            continue
        name, f = _pick_name(faces, names, refs)
        if name is None:
            continue
        roi = _torso_roi(fr, f["box"])
        if roi is None:
            continue
        sig = _color_sig(roi)
        sig["frame"] = idx
        first_sigs[name].append(sig)
        first_frames[name].append(idx)

    for k in range(n_windows):
        for frac in (0.15, 0.5, 0.85):
            idx = int(total * ((k + frac) / n_windows))
            fr = _read_at(cap, idx)
            if fr is None:
                continue
            faces = [f for f in engine.faces(fr)
                     if f["size"] >= 40 and f["frontal"] >= 0.35]
            if not faces:
                continue
            faces.sort(key=lambda z: -z["size"])
            for f in faces:
                roi = _torso_roi(fr, f["box"])
                if roi is None:
                    continue
                name, _ = _pick_name([f], names, refs)
                if name is None:
                    continue
                sig = _color_sig(roi)
                sig["frame"] = idx
                wins[name].append((k, sig))
    out = {}
    for name in names:
        rows = wins[name]
        by_win = {k: [] for k in range(n_windows)}
        for k, sig in rows:
            by_win[k].append(sig)
        win_sigs = {k: _median_sig(v) for k, v in by_win.items() if v}
        if first_sigs[name]:
            first = _median_sig(first_sigs[name][:3])
            first_win = None
            baseline_source = "frames_%s" % ",".join(str(x) for x in first_frames[name][:3])
        else:
            first = win_sigs.get(0)
            ks = sorted(win_sigs)
            if first is None and ks:
                first = win_sigs[ks[0]]
            first_win = ks[0] if ks else None
            baseline_source = "window_%s" % first_win
        body_sigs = [win_sigs[k] for k in sorted(win_sigs)
                     if first_win is None or k > first_win]
        body = _median_sig(body_sigs) if body_sigs else None
        out[name] = {
            "costume_id": (seg.get("costume") or {}).get(name),
            "baseline_window": first_win,
            "baseline_source": baseline_source,
            "baseline": first,
            "body": body,
            "delta": _costume_delta(first, body),
            "windows": [{"window": k, **win_sigs[k]} for k in sorted(win_sigs)],
            "n_frames": len(rows),
        }
    for name in skipped:
        out[name] = {
            "costume_id": (seg.get("costume") or {}).get(name),
            "baseline": None, "body": None, "delta": None, "windows": [],
            "n_frames": 0,
            "skipped_reason": "多人段存在无参考图角色，无法可靠归属躯干 ROI",
        }
    return out


def _costume_flags(cost_res, seg_idx, prof):
    seg = _seg_conf(prof, seg_idx)
    flags = []
    chars = cost_res.get("characters") if isinstance(cost_res, dict) else None
    if not isinstance(chars, dict):
        chars = cost_res if isinstance(cost_res, dict) else {}
    for name, r in chars.items():
        if not isinstance(r, dict):
            continue
        base, body, d = r.get("baseline"), r.get("body"), r.get("delta")
        if not base or not body or not d:
            flags.append({
                "code": "COSTUME_UNVERIFIABLE", "character": name, "severity": "low",
                "detail": "没有可用的躯干 ROI（人脸太小/出画/遮挡），无法核验服装。",
            })
            continue
        if seg.get("forbid_added_layers") and (
            abs(d["white"]) >= TH["costume_white"] or
            abs(d["dark"]) >= TH["costume_dark"] or
            abs(d["L"]) >= TH["costume_L"]
        ):
            flags.append({
                "code": "COSTUME_JUMP_AFTER_FIRST_FRAME", "character": name, "severity": "high",
                "detail": (
                    f"首帧基线→段内躯干色彩变化 white {d['white']:+.2f} / dark {d['dark']:+.2f} / "
                    f"L {d['L']:+.1f}；提示词要求延续首帧服装，疑似段内加衣/换装。"
                ),
            })
        # 窗间突变：亮度跳变且黑白占比同向变化
        ws = sorted(r.get("windows") or [], key=lambda z: z["window"])
        for a, b in zip(ws, ws[1:]):
            dL = abs(b["L"] - a["L"])
            dd = abs(b["dark"] - a["dark"])
            dw = abs(b["white"] - a["white"])
            if dL >= TH["costume_window_L"] and (dd >= 0.15 or dw >= 0.15):
                flags.append({
                    "code": "COSTUME_WINDOW_JUMP", "character": name, "severity": "low",
                    "detail": f"窗 {a['window']}→{b['window']} 亮度变化 {dL:.1f}，黑白占比变化 dark {b['dark']-a['dark']:+.2f}/white {b['white']-a['white']:+.2f}；切镜/打光也会造成同样变化，仅咨询。",
                })
                break
    return flags


# ────────────────────────────── 音色 ──────────────────────────────
def _extract_wav(video, wav):
    ff = ffmpeg_path()
    if not ff:
        return False, "找不到 ffmpeg（设置 H3_GUARD_FFMPEG）"
    os.makedirs(os.path.dirname(os.path.abspath(wav)) or ".", exist_ok=True)
    r = subprocess.run(
        [ff, "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000", wav],
        capture_output=True, text=True, creationflags=0x08000000,
    )
    if r.returncode != 0 or not os.path.isfile(wav):
        return False, (r.stderr or "")[-200:]
    return True, ""


def _f0_stats(wav):
    np = _import_cv2()[1]
    try:
        w = wave.open(wav, "rb")
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        w.close()
    except Exception as e:
        return {"error": str(e)}
    win, hop = int(0.04 * sr), int(0.02 * sr)
    vals = []
    for i in range(0, max(0, len(x) - win), hop):
        fr = x[i:i + win]
        if float(np.sqrt(np.mean(fr ** 2))) < 0.01:
            continue
        fr = fr - fr.mean()
        ac = np.correlate(fr, fr, "full")[win - 1:]
        lo, hi = int(sr / 400), int(sr / 60)
        if hi >= len(ac):
            continue
        k = int(np.argmax(ac[lo:hi]) + lo)
        if ac[k] > 0.3 * ac[0]:
            vals.append(sr / k)
    if not vals:
        return {"voiced": 0, "f0_median": None, "f0_q1": None, "f0_q3": None,
                "female_ratio": 0.0, "male_ratio": 0.0}
    arr = np.array(vals, dtype=np.float32)
    female = float(((arr >= 165) & (arr <= 320)).mean())
    male = float(((arr >= 80) & (arr < 165)).mean())
    return {
        "voiced": len(vals),
        "f0_median": round(float(np.median(arr)), 1),
        "f0_q1": round(float(np.percentile(arr, 25)), 1),
        "f0_q3": round(float(np.percentile(arr, 75)), 1),
        "female_ratio": round(female, 3),
        "male_ratio": round(male, 3),
    }


def _voice_flags(voice, seg_idx, prof):
    seg = _seg_conf(prof, seg_idx)
    flags = []
    if not voice or voice.get("voiced", 0) < TH["voice_min_voiced"]:
        return [{
            "code": "VOICE_UNVERIFIABLE", "severity": "low",
            "detail": "有效浊音帧不足，无法做基频检查。",
        }]
    med = voice.get("f0_median")
    speakers = [s for s in (seg.get("speakers") or []) if s in (prof.get("characters") or {})]
    ranged = [(s, prof["characters"][s].get("f0_range")) for s in speakers]
    ranged = [(s, r) for s, r in ranged if isinstance(r, list) and len(r) == 2]
    if len(speakers) == 1 and ranged:
        name, (lo, hi) = ranged[0]
        if med is not None and (med < lo * 0.88 or med > hi * 1.12):
            flags.append({
                "code": "VOICE_BAND_MISMATCH", "speaker": name, "severity": "medium",
                "detail": f"本段只有 {name} 说话，F0 中位数={med}Hz，超出设定频带 {lo}-{hi}Hz；疑似音色漂移（环境音可能干扰）。",
            })
    elif ranged:
        for name, (lo, hi) in ranged:
            band = voice.get("male_ratio") if hi <= 165 else voice.get("female_ratio")
            if band is not None and band < TH["voice_band_min"]:
                flags.append({
                    "code": "VOICE_BAND_COVERAGE_LOW", "speaker": name, "severity": "low",
                    "detail": f"本段 {name} 设定频带 {lo}-{hi}Hz 的浊音覆盖率仅 {band:.0%}；多人段仅咨询。",
                })
    return flags


# ────────────────────────────── 跨段 ──────────────────────────────
def _seg_index_from_path(path, fallback):
    m = re.search(r"seg[_-]?(\d+)", os.path.basename(path), re.I)
    return int(m.group(1)) if m else int(fallback)


def _aggregate_verdict(flags, verified=False):
    sev = {f.get("severity") for f in flags}
    if "high" in sev:
        return "fail"
    if "medium" in sev:
        return "review"
    unv = any(str(f.get("code", "")).endswith("UNVERIFIABLE") for f in flags)
    if unv and not verified:
        return "unverifiable"
    return "pass"


def should_retry(seg_result, policy="balanced"):
    """编排层唯一重试判定入口。balanced 只对 high；strict 对 high+medium。"""
    if not seg_result:
        return False
    if seg_result.get("retry_recommended"):
        return True
    verdict = seg_result.get("verdict")
    if verdict == "fail":
        return True
    if policy == "strict" and verdict == "review":
        return True
    return False


def check_segments(clip_paths, prof, workdir, sample_n=32, voice=True):
    """对一组分段做只读检查，返回报告 dict；所有中间帧/wav 落在 workdir。"""
    engine = _FaceEngine()
    files = [os.path.abspath(p) for p in clip_paths if os.path.isfile(p)]
    if not files:
        raise ValueError("没有可检查的视频文件")
    os.makedirs(workdir, exist_ok=True)
    refs = {}
    ref_meta = {}
    for name, c in (prof.get("characters") or {}).items():
        ref = c.get("face_ref")
        if ref and os.path.isfile(ref):
            try:
                refs[name] = engine.ref_embedding(ref)
                ref_meta[name] = {"path": ref, "ok": True}
            except Exception as e:
                ref_meta[name] = {"path": ref, "ok": False, "error": str(e)}
        else:
            ref_meta[name] = {"path": ref, "ok": False, "error": "未配置或文件不存在"}
    report = {
        "guard_version": VERSION,
        "generated_at": _now(),
        "profile": prof.get("_path"),
        "film": prof.get("film"),
        "workdir": os.path.abspath(workdir),
        "references": ref_meta,
        "reference_audio": reference_audio_status(prof),
        "known_blind_spots": prof.get("known_blind_spots") or [],
        "thresholds": TH,
        "boundaries": BOUNDARIES,
        "segments": {},
    }
    ordered = []
    for fallback, path in enumerate(files):
        idx = _seg_index_from_path(path, fallback)
        ordered.append((idx, path))
    ordered.sort()
    for idx, path in ordered:
        seg = _seg_conf(prof, idx)
        cap, total, w, h = _video_reader(path)
        if cap is None:
            report["segments"][f"{idx:02d}"] = {
                "path": path, "verdict": "unverifiable",
                "flags": [{"code": "VIDEO_UNREADABLE", "severity": "low", "detail": "OpenCV 打不开视频"}],
                "retry_recommended": False,
            }
            continue
        t0 = time.time()
        face_res = _face_analysis(engine, path, prof, idx, total, cap, refs, n=sample_n)
        cost_res = _costume_analysis(engine, path, prof, idx, total, cap, refs)
        voice_res = None
        if voice:
            wav = os.path.join(workdir, f"seg_{idx:02d}.wav")
            ok, err = _extract_wav(path, wav)
            voice_res = _f0_stats(wav) if ok else {"error": err, "voiced": 0}
            try:
                os.remove(wav)
            except OSError:
                pass
        flags = []
        flags += _face_flags(face_res, idx, prof)
        flags += _costume_flags(cost_res, idx, prof)
        if voice:
            flags += _voice_flags(voice_res, idx, prof)
        verified_face = any(
            r.get("n", 0) > 0 and r.get("ref_sim") is not None
            and r["ref_sim"] >= TH["face_ref_med"]
            for r in (face_res.get("characters") or {}).values()
        )
        verdict = _aggregate_verdict(flags, verified=verified_face)
        report["segments"][f"{idx:02d}"] = {
            "path": path,
            "size": f"{w}x{h}",
            "frames": total,
            "elapsed_s": round(time.time() - t0, 2),
            "costume_id": seg.get("costume"),
            "speakers": seg.get("speakers"),
            "faces": face_res,
            "costume": cost_res,
            "voice": voice_res,
            "flags": flags,
            "verdict": verdict,
            "retry_recommended": verdict == "fail",
        }
        cap.release()

    # 跨段服装：仅在同一角色、同一 costume_id 的相邻段之间比较
    keys = sorted(report["segments"])
    for a, b in zip(keys, keys[1:]):
        sa, sb = report["segments"][a], report["segments"][b]
        if int(b) - int(a) > 2:
            continue
        for name, ra in (sa.get("costume") or {}).items():
            rb = (sb.get("costume") or {}).get(name)
            if not rb or not ra.get("body") or not rb.get("body"):
                continue
            if not ra.get("costume_id") or ra.get("costume_id") != rb.get("costume_id"):
                continue
            d = _costume_delta(ra["body"], rb["body"])
            if abs(d["L"]) >= TH["costume_cross_L"] or abs(d["dark"]) >= TH["costume_cross_dark"]:
                sb.setdefault("flags", []).append({
                    "code": "CROSS_SEGMENT_COSTUME_DRIFT", "character": name, "severity": "medium",
                    "detail": f"与 seg_{int(a):02d} 同为 {ra['costume_id']}，躯干色彩 L {d['L']:+.1f}/dark {d['dark']:+.2f}，疑似同款服装漂移。",
                })
                sb["verdict"] = _aggregate_verdict(sb["flags"])
    summary = {"pass": [], "review": [], "unverifiable": [], "fail": []}
    for k, s in report["segments"].items():
        summary.setdefault(s["verdict"], []).append(k)
    report["summary"] = summary
    report["retry_segments"] = [k for k, s in report["segments"].items() if s.get("retry_recommended")]
    return report


# ────────────────────────────── 输出 ──────────────────────────────
def render_markdown(report):
    lines = [
        f"# H3 一致性守卫问题清单 · {report.get('film') or ''}",
        "",
        f"- 生成时间：{report.get('generated_at')}",
        f"- profile：`{report.get('profile')}`",
        f"- 守卫版本：{report.get('guard_version')}",
        "- 检查方式：只读抽帧/抽音频；未重跑任何场景，未修改成片与分段。",
        "",
        "## 逐段结论",
        "",
        "| 段 | 结论 | 触发标记 | 证据 | 建议 |",
        "|---|---|---|---|---|",
    ]
    for k in sorted(report["segments"]):
        s = report["segments"][k]
        flags = s.get("flags") or []
        codes = "<br>".join(f"{f.get('code')}({f.get('severity')})" for f in flags) or "—"
        ev = []
        for name, r in (s.get("faces", {}).get("characters") or {}).items():
            ev.append(f"{name} face n={r['n']} sim={r['ref_sim']}")
        for name, r in (s.get("costume") or {}).items():
            d = r.get("delta") or {}
            ev.append(f"{name} costume Δwhite={d.get('white')} Δdark={d.get('dark')} ΔL={d.get('L')}")
        v = s.get("voice") or {}
        if v.get("f0_median") is not None:
            ev.append(f"F0={v['f0_median']}Hz female={v.get('female_ratio')} male={v.get('male_ratio')}")
        advice = "换 seed 重跑 + 使用修复后的提示词" if s.get("retry_recommended") else (
            "人工复核" if s.get("verdict") == "review" else
            ("补参考图/更近景别后可复核" if s.get("verdict") == "unverifiable" else "—")
        )
        lines.append(f"| {k} | {s.get('verdict')} | {codes} | {'；'.join(ev) or '—'} | {advice} |")
    lines += ["", "## 证据说明", ""]
    for k in sorted(report["segments"]):
        s = report["segments"][k]
        if not s.get("flags"):
            continue
        lines.append(f"### seg_{k}")
        for f in s["flags"]:
            who = f.get("character") or f.get("speaker") or ""
            lines.append(f"- **{f.get('code')}**（{f.get('severity')}{' · ' + who if who else ''}）：{f.get('detail')}")
        lines.append("")
    blind = report.get("known_blind_spots") or []
    if blind:
        lines += ["## 已知盲区 / 需人工复核", ""]
        for b in blind:
            lines.append(f"- {b}")
        lines.append("")
    lines += ["## 能力边界（不夸大）", ""]
    for b in report.get("boundaries") or BOUNDARIES:
        lines.append(f"- {b}")
    return "\n".join(lines) + "\n"


def doctor():
    cv2_ok = cv2_err = None
    try:
        cv2, np = _import_cv2()
        cv2_ok = {"cv2": cv2.__version__, "numpy": np.__version__}
    except Exception as e:
        cv2_err = str(e)
    md = model_dir()
    return {
        "version": VERSION,
        "python": sys.executable,
        "cv2": cv2_ok,
        "cv2_error": cv2_err,
        "model_dir": md,
        "yunet": os.path.isfile(os.path.join(md, YUNET_NAME)),
        "sface": os.path.isfile(os.path.join(md, SFACE_NAME)),
        "ffmpeg": ffmpeg_path(),
        "boundaries": BOUNDARIES,
    }


# ────────────────────────────── CLI ──────────────────────────────
def _collect_clips(arg, only=None):
    if os.path.isdir(arg):
        files = sorted(glob.glob(os.path.join(arg, "*.mp4")))
    else:
        files = [p.strip() for p in arg.split(",") if p.strip()]
    if only:
        keep = {int(x) for x in re.split(r"[,\s]+", only) if x.strip().isdigit()}
        files = [p for p in files if _seg_index_from_path(p, -1) in keep]
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description="H3 一致性守卫")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor")
    d.add_argument("--json", action="store_true")
    p = sub.add_parser("prompt")
    p.add_argument("--profile", required=True)
    p.add_argument("--segment", type=int, required=True)
    p.add_argument("--base-file")
    p.add_argument("--base", default="")
    p.add_argument("--repair", action="store_true")
    p.add_argument("--out")
    c = sub.add_parser("check")
    c.add_argument("--profile", required=True)
    c.add_argument("--clips", required=True, help="目录或逗号分隔文件")
    c.add_argument("--only", default="")
    c.add_argument("--workdir", required=True)
    c.add_argument("--sample-n", type=int, default=32)
    c.add_argument("--no-voice", action="store_true")
    c.add_argument("--out", required=True)
    c.add_argument("--markdown")
    a = ap.parse_args(argv)
    if a.cmd == "doctor":
        info = doctor()
        print(json.dumps(info, ensure_ascii=False, indent=1) if a.json else
              "\n".join(f"{k}: {v}" for k, v in info.items()))
        return 0
    if a.cmd == "prompt":
        prof = load_profile(a.profile)
        base = a.base
        if a.base_file:
            with open(a.base_file, encoding="utf-8") as f:
                base = f.read()
        res = build_prompt(base, prof, a.segment, repair=a.repair)
        if a.out:
            _dump_json(a.out, res)
            print(a.out)
        else:
            print(res["prompt"])
            if res["warnings"]:
                print("\nWARNINGS: " + json.dumps(res["warnings"], ensure_ascii=False), file=sys.stderr)
        return 0
    if a.cmd == "check":
        prof = load_profile(a.profile)
        files = _collect_clips(a.clips, a.only)
        rep = check_segments(files, prof, a.workdir, sample_n=a.sample_n, voice=not a.no_voice)
        _dump_json(a.out, rep)
        if a.markdown:
            with open(a.markdown, "w", encoding="utf-8") as f:
                f.write(render_markdown(rep))
        print(json.dumps(rep["summary"], ensure_ascii=False))
        print("report:", a.out)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
