# -*- coding: utf-8 -*-
r"""H3 出片质量裁判：NeoHorse-Jev-4B 视觉决策 × ComfyUI/H3 链路

用法:
  python h3_judge.py --check          # 验证整条链路（不加载模型权重，随时可跑）
  python h3_judge.py --latest         # 裁判 H:\h3cache 最新视频（要求 ComfyUI 空闲）
  python h3_judge.py --video <路径>   # 裁判指定视频

安全设计:
  - ComfyUI 正在生成时拒绝加载模型（防止 32G 内存被打爆）
  - 默认 device=cpu（8G 显存塞不下 9GB 权重；裁判在生成间隙跑，CPU 足够）
"""
import os
import sys
import json
import glob
import time
import hashlib
import argparse
import subprocess
import urllib.request
from pathlib import Path

FFMPEG = r"D:\ComfyUI\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
MODEL_DIR = r"H:\JEV\NeoHorse-Jev-4B"
CACHE = r"H:\h3cache"
COMFY = "http://127.0.0.1:8188"
LOG = r"H:\h3cache\h3_cache\judge.log"
FRAMES_DIR = r"H:\h3cache\h3_cache\judge_frames"
MAX_FRAMES = 8
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

QUALITY_Q = {
    "state": "This is one frame from an AI-generated anime video. Judge its visual quality.",
    "questions": {
        "quality": {
            "type": "score",
            "instructions": "Rate the overall visual quality of this anime frame: good = clean and coherent; flawed = minor artifacts or blur; broken = deformed face/limbs or corrupted artifacts.",
            "criteria": ["broken", "flawed", "good"],
        },
    },
}

# 自带字幕检测：H3 偶尔会把台词直接烧进画面；检测到就跳过后期字幕烧录，避免叠字
SUBTITLE_Q = {
    "state": "This is one frame from an AI-generated short drama video. Look at the bottom area of the frame.",
    "questions": {
        "subtitle": {
            "type": "choice",
            "instructions": "Does this frame contain burned-in subtitle text at the bottom (Chinese characters overlaid on the video as captions, not text that belongs to the scene)? Choose yes only when caption text is clearly visible.",
            "criteria": {"no": "No subtitle overlay is visible.",
                         "yes": "A subtitle caption overlay is visible."},
        },
    },
}


def log(m):
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def comfy_busy():
    """True=正在生成或已有排队任务(禁加载) / False=空闲 / None=服务器没开(视为空闲)
    pending 也要算：排队任务下一秒就会开始生成并加载模型，此时裁判再压 9GB 进内存就撞车"""
    try:
        with urllib.request.urlopen(COMFY + "/queue", timeout=5) as r:
            d = json.load(r)
        return len(d.get("queue_running", [])) + len(d.get("queue_pending", [])) > 0
    except Exception:
        return None


def latest_video():
    vids = [p for p in glob.glob(os.path.join(CACHE, "*.mp4"))]
    return max(vids, key=os.path.getmtime) if vids else None


def extract_frames(video, outdir, n=MAX_FRAMES):
    os.makedirs(outdir, exist_ok=True)
    for f in glob.glob(os.path.join(outdir, "frame_*.png")):
        os.remove(f)
    cmd = [FFMPEG, "-y", "-i", video,
           "-vf", f"fps=1/1.5,scale=1280:-2", "-frames:v", str(n),
           os.path.join(outdir, "frame_%02d.png")]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       creationflags=CREATE_NO_WINDOW)
    frames = sorted(glob.glob(os.path.join(outdir, "frame_*.png")))
    return frames if r.returncode == 0 else []


def sha256_of(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 22)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------- 链路自检（不加载权重） ----------------
def cmd_check():
    results = []

    def rec(name, ok, note=""):
        results.append((name, ok, note))
        print(f"  {'✓' if ok else '✗'} {name}  {note}")

    print("== JEV 4B × ComfyUI/H3 链路自检 ==")

    print("[1] 运行时")
    try:
        from neohorse_decision import DecisionEngine  # noqa
        from neohorse_decision.vision import VisionDecisionEngine  # noqa
        rec("neohorse_decision 导入", True)
    except Exception as e:
        rec("neohorse_decision 导入", False, str(e)[:90])

    print("[2] 模型文件")
    bb = Path(MODEL_DIR) / "backbone"
    shards = sorted(bb.glob("model-*.safetensors"))
    head = Path(MODEL_DIR) / "pointer_head.safetensors"
    rec("backbone 分片×3", len(shards) == 3, f"找到 {len(shards)} 个")
    rec("决策头 pointer_head", head.exists(), f"{head.stat().st_size/1024/1024:.1f}MB" if head.exists() else "缺失")

    print("[3] 决策头 SHA256 抽查（5MB，秒级）")
    ok_hash = False
    try:
        want = None
        for line in open(Path(MODEL_DIR) / "SHA256SUMS", encoding="utf-8", errors="ignore"):
            if "pointer_head" in line:
                want = line.split()[0]
                break
        if want:
            got = sha256_of(head)
            ok_hash = got.lower() == want.lower()
            rec("pointer_head 哈希匹配", ok_hash, f"{got[:12]}… vs {want[:12]}…")
        else:
            rec("pointer_head 哈希匹配", False, "SHA256SUMS 里没找到条目")
    except Exception as e:
        rec("pointer_head 哈希匹配", False, str(e)[:90])

    print("[4] ffmpeg 抽帧（对 H 盘现有视频实测）")
    vid = latest_video()
    if vid:
        frames = extract_frames(vid, FRAMES_DIR, n=2)
        rec("视频→关键帧", len(frames) >= 1, f"{len(frames)} 帧 ← {os.path.basename(vid)}")
    else:
        rec("视频→关键帧", False, "H:\\h3cache 没有任何视频")

    print("[5] ComfyUI 队列接口（空闲检测联动）")
    busy = comfy_busy()
    if busy is True:
        rec("队列接口", True, "可达；当前正在生成（裁判此时会拒绝加载）")
    elif busy is False:
        rec("队列接口", True, "可达；当前空闲")
    else:
        rec("队列接口", False, "服务器无响应（ComfyUI 没开也能裁判，此项忽略）")

    print()
    allok = all(ok for _, ok, _ in results)
    print("★ 链路状态:", "全通 —— 可以点火（等 ComfyUI 空闲时跑 --latest）" if allok else "存在断点，见上")
    return 0 if allok else 1


# ---------------- 正式裁判 ----------------
def cmd_judge(video):
    busy = comfy_busy()
    if busy is True:
        print("⛔ ComfyUI 正在生成。为防内存冲突，裁判拒绝加载模型。等生成结束后再跑。")
        log("拒绝加载：ComfyUI 忙")
        return 3

    frames = extract_frames(video, FRAMES_DIR)
    if not frames:
        print("✗ 抽帧失败")
        return 1
    log(f"裁判 {os.path.basename(video)}：抽了 {len(frames)} 帧")

    # 复查互斥闸：抽帧耗时数秒，期间 ComfyUI 可能已开始生成；模型加载要 1-3 分钟、
    # 加载开始后就拦不住了，只能在提交内存前把竞态窗口压到最小
    if comfy_busy() is True:
        print("⛔ 抽帧间隙 ComfyUI 开始生成，裁判放弃加载模型。等生成结束再跑。")
        log("拒绝加载：抽帧后复查 ComfyUI 忙")
        return 3

    import torch
    from PIL import Image
    from neohorse_decision.vision import VisionDecisionEngine

    device = "cuda" if os.environ.get("JEV_DEVICE") == "cuda" else "cpu"
    log(f"加载 4B 裁判模型（device={device}），首次约 1-3 分钟...")
    eng = VisionDecisionEngine(MODEL_DIR, device=device)

    per_frame = []
    for fp in frames:
        img = Image.open(fp)
        if img.width * img.height > 4_000_000:
            img = img.resize((img.width // 2, img.height // 2))
        try:
            rec = eng.predict(QUALITY_Q, image=img)
        except Exception as e:
            per_frame.append({"frame": os.path.basename(fp), "error": str(e)[:120]})
            continue
        a = rec.get("answers", {})
        q = a.get("quality", {})
        # score 题的返回：probabilities = {"0": p_broken, "1": p_flawed, "2": p_good}
        probs = q.get("probabilities") or {}
        criteria = ["broken", "flawed", "good"]
        # 兜底先落位：概率格式异常时退回 answer/label，别让整次裁判崩在权重加载完之后
        grade = q.get("answer") or q.get("label") or "?"
        conf = q.get("confidence", "?")
        if probs:
            try:
                best = max(probs, key=lambda k: float(probs[k]))
                grade = criteria[int(best)]
                conf = round(float(probs[best]), 3)
            except (ValueError, TypeError, KeyError, IndexError):
                pass  # 概率非数值/序号越界/键不可解析 → 用上面的兜底值
        sub_choice = None
        try:
            rec_s = eng.predict(SUBTITLE_Q, image=img)
            sub_choice = ((rec_s.get("answers") or {}).get("subtitle") or {}).get("choice")
        except Exception:
            sub_choice = None
        per_frame.append({
            "frame": os.path.basename(fp),
            "quality": grade,
            "confidence": conf,
            "subtitle": sub_choice,
        })
        print(f"  {os.path.basename(fp)}: quality={grade} ({conf}) subtitle={sub_choice}")

    order = {"broken": 0, "flawed": 1, "good": 2}
    grades = [p.get("quality") for p in per_frame if p.get("quality") in order]
    mean = sum(order[g] for g in grades) / len(grades) if grades else -1
    verdict = "good" if mean >= 1.7 else ("flawed" if mean >= 0.7 else "broken")
    subs = [p.get("subtitle") for p in per_frame if p.get("subtitle") in ("yes", "no")]
    has_subtitle = (subs.count("yes") >= 2) if subs else None
    report = {
        "video": video,
        "device": device,
        "frames_judged": len(per_frame),
        "grades": grades,
        "mean_score": round(mean, 2),
        "verdict": verdict,
        "has_subtitle": has_subtitle,
        "subtitle_votes": {"yes": subs.count("yes"), "no": subs.count("no")},
        "per_frame": per_frame,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    out = os.path.splitext(video)[0] + "_judge.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    log(f"裁定: {verdict}（均分 {mean:.2f}）→ {os.path.basename(out)}")
    print(f"★ 裁定: {verdict}  报告: {out}")
    return 0 if verdict == "good" else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--video")
    args = ap.parse_args()

    if args.check:
        sys.exit(cmd_check())
    video = args.video or (args.latest and latest_video())
    if not video:
        print("H:\\h3cache 没有视频可裁判")
        sys.exit(1)
    sys.exit(cmd_judge(os.path.abspath(video)))


if __name__ == "__main__":
    main()
