# H3 导演台（video_pipeline.py）部署适配指南

本目录是 `D:\H3DirectorDesk\video_pipeline.py` 的可部署副本（逐字节一致，md5 `6accc9becf52c6b74a73e607113a7194`；未发现密钥，未做任何脱敏改动）。
新机器部署时：**原始文件不改**，改副本中的路径常量，并按下表准备依赖环境。

---

## 1. 部署前提

### Python 与依赖包

服务本体只用标准库（`http.server` / `urllib` / `wave` 等，见副本 17–30 行），但音色/字幕/守卫功能需要以下三方包（在运行服务的解释器环境里安装）：

| 包 | 实测版本 | 用途 |
|---|---|---|
| numpy | 2.5.2 | 抽取 16k 音频做 Whisper 对齐（副本 1991 行） |
| Pillow | 12.3.0 | 字体测量/字幕换行（副本 2006 行） |
| faster-whisper | 1.2.1 | 字幕识别（副本 2107 行，本地模型路径见下表） |

```bash
pip install numpy pillow faster-whisper imageio-ffmpeg
```

- 原机解释器：`D:\ComfyUI\venv\Scripts\pythonw.exe`（Python 3.13.3）。选它是因为该 venv 同时是 ComfyUI 的环境，模型/ffmpeg 都现成；新机器任何 Python ≥3.10 装齐上表包即可。
- `imageio-ffmpeg` 非必须：它只是 ffmpeg 可执行文件的一个来源（见下）。

### ffmpeg 来源

副本第 48 行 `FFMPEG` 指向 `imageio_ffmpeg` 包自带的 Windows 静态二进制（v7.1）。新机器三选一：

1. `pip install imageio-ffmpeg`，把 `FFMPEG` 改成 `<venv>\Lib\site-packages\imageio_ffmpeg\binaries\` 包内实际的可执行文件（原机为 `ffmpeg-win-x86_64-v7.1.exe`；**文件名带版本号、随包版本变化，以你机器上 binaries 目录里的实际文件名为准**）；
2. 自行安装 ffmpeg 并加入 PATH，`FFMPEG` 改成 `"ffmpeg"`；
3. 守卫工具 `tools/h3_guard.py` 另支持环境变量 `H3_GUARD_FFMPEG` 指定 ffmpeg 路径（`tools/h3_guard.py:98`）。

### 程序目录伴随文件（与 video_pipeline.py 同放一个程序目录）

副本运行时还会从自身所在程序目录（常量 `DL`）加载以下内容，缺了对应功能会降级或报错：

- `dialogue.json` —— 全局台词/风格配置（**必须**，处理任务时 `load_dialogue` 直接打开，副本 1986 行）。模板见本目录 `config.example.json`。
- `h3_film_seg_template.json` —— 长片（film）模式的 ComfyUI API 工作流模板（副本 64 行加载）。**本目录已随仓库提供**（`service/h3_film_seg_template.json`，与原机 `D:\H3DirectorDesk` 内原件 md5 一致：`8da080fa51d87454b34299ff74098617`），部署时直接复制到程序目录即可。模板内模型引用全部是**裸文件名**（由 ComfyUI 模型目录解析，见 README §4 放置位置），无任何机器路径需要修改；流水线每段会改写其中 5 个节点（`114` LoadImage 首帧、`104` 提示词、`15` 种子、`111` 时长秒、`92` SaveVideo 前缀，副本 700–705 行），模板里现有的示例值（`example.png`、示例 prompt、`filmseg_同一场雨` 前缀）都会被运行时覆盖。不用长片功能可不带。
- `tools/` 子目录 —— **本目录（service/tools/）已随仓库提供**（`h3_guard.py` 生成前提示词守卫、`h3_voice.py` Seed-VC 音色转换【需独立 venv/CLI】、`h3_prompt_skill.py` 可选提示词封装，附 `h3_voice_profiles/`、`h3_guard_profiles/` 两份示例档案；均与开发机原件一致）。部署时复制到程序目录 `tools\`。不用音色/守卫功能可整目录不带（代码按「跳过」处理）。

### 外部服务

| 服务 | 地址 | 说明 |
|---|---|---|
| ComfyUI | `http://127.0.0.1:8188`（副本 56 行 `COMFY`） | 出图/出片引擎，需预装 H3 相关工作流节点 |
| 音乐引擎（ACE-Step） | `http://127.0.0.1:8001/health`（副本 2249 行） | 由副本按需自动拉起（`start_engine_gpu.bat`） |
| JEV 决策 | `H:\JEV\venv\Scripts\python.exe` 跑 `h3_judge.py`/`video_decide.py`/`storyboard_decide.py` | 独立 venv。三个粘合脚本已随本目录 `service/jev/` 提供；运行时 `neohorse_decision` 与模型权重从 [NeoHorse-Jev-4B](https://huggingface.co/TokenRhythm/NeoHorse-Jev-4B) 发布包获取（发布包自带 wheel/源码），部署步骤见 README §5.2 第五步 |

---

## 2. 需要改的路径（副本内逐条）

以下路径全部硬编码在副本开头常量区（34–77 行）或函数内，新机器按下表改：

| 审计路径 | 副本位置 | 含义 | 建议改成 |
|---|---|---|---|
| `C:\Windows\Fonts\Noto Sans SC.ttf` | 2037 行 | H3 风格字幕字体 | 新机字体绝对路径，或部署 Noto Sans SC 后保持 |
| `C:\Windows\Fonts\SIMLI.TTF` | 133 行（FONTS 隶书） | 中文字体（隶书） | 新机对应字体路径；缺字体时代码回退雅黑（2010 行） |
| `C:\Windows\Fonts\msyhbd.ttc` | 128 行（FONTS 雅黑） | 中文字体（微软雅黑粗体，默认字体） | 同上 |
| `C:\Windows\Fonts\simfang.ttf` | 132 行（仿宋） | 中文字体 | 同上 |
| `C:\Windows\Fonts\simhei.ttf` | 129 行（黑体） | 中文字体 | 同上 |
| `C:\Windows\Fonts\simkai.ttf` | 130 行（楷体） | 中文字体 | 同上 |
| `C:\Windows\Fonts\simsun.ttc` | 131 行（宋体） | 中文字体 | 同上 |
| `D:\ComfyUI\input` | 71 行 `COMFY_INPUT` | ComfyUI LoadImage 输入图目录（首帧直落此处） | 新机 ComfyUI 的 input 目录 |
| `D:\ComfyUI\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe` | 48 行 `FFMPEG` | ffmpeg 可执行文件 | 见上文「ffmpeg 来源」 |
| `D:\ComfyUI\venv\Scripts\pythonw.exe` | 不在 py 内，在 restart_pipeline.example.ps1 第 8 行 | 运行服务的解释器（无窗后台运行） | 新机解释器的 pythonw.exe |
| `D:\H3DirectorDesk` | 39 行 `DL` | 程序/配置目录（dialogue.json、tools、模板都在此） | 新机程序目录，如 `C:\H3DirectorDesk` |
| `D:\H3DirectorDesk\restart_pipeline.ps1` | 不在 py 内（本目录 example ps1 即其副本） | 重启脚本 | 部署后的新脚本路径 |
| `D:\H3DirectorDesk\tools\h3_voice_profiles` | 76 行 `VOICE_PROFILE_DIRS[1]` | 音色档案目录（D 盘备份位） | 新机档案目录；与 H 盘主位成对，保留两个候选 |
| `D:\H3DirectorDesk\video_pipeline.py` | restart_pipeline.example.ps1 第 3、9 行 | 服务脚本匹配/启动路径 | 新机副本路径 |
| `E:\local-models\huggingface\hub\models--Systran--faster-whisper-small\snapshots\536b0662...` | 2108 行 `wm` | faster-whisper-small 本地模型（离线加载） | 新机模型快照目录，或改成 `"small"` 让其自动下载（受 `HF_ENDPOINT` 镜像影响） |
| `H:/MusicAI/scripts/start_engine_gpu.bat` | 2258 行 | 音乐引擎离线时自动拉起脚本 | 新机 MusicAI 部署后的对应脚本 |
| `H:\H3DirectorDesk\tools\h3_voice_profiles` | 75 行 `VOICE_PROFILE_DIRS[0]` | 音色档案目录（H 盘主位） | 同上成对修改 |
| `H:\JEV\h3_judge.py` | 49 行 `JUDGE_SCRIPT` | JEV 语义判定脚本 | 新机 JEV 部署路径 |
| `H:\JEV\storyboard_decide.py` | 63 行 `STORYBOARD_SCRIPT` | 长片分镜决策脚本 | 同上 |
| `H:\JEV\venv\Scripts\python.exe` | 50 行 `JEV_PY` | JEV 独立解释器 | 同上 |
| `H:\JEV\video_decide.py` | 51 行 `DECIDE_SCRIPT` | 单片决策脚本 | 同上 |
| `H:\MusicAI` | 52–55 行相关 | 音乐生成工程根 | 新机 MusicAI 根目录 |
| `H:\MusicAI\apps\ACE-Step-1.5` | start_engine_gpu.bat 内部引用 | ACE-Step 引擎代码 | 随 MusicAI 一并部署 |
| `H:\MusicAI\apps\ACE-Step-1.5\checkpoints` | 同上 | ACE-Step 模型权重 | 同上 |
| `H:\MusicAI\cache\hf` / `cache\ms` / `cache\tmp` | MusicAI 侧缓存（引擎脚本使用） | HF/MS 模型缓存 | 随 MusicAI 配置 |
| `H:\MusicAI\envs\acestep\Scripts\python.exe` | 53 行 `ACE_PY` | ACE-Step 出曲解释器 | 新机对应 venv |
| `H:\MusicAI\outputs` | 55 行 `BGM_DIRS[1]` | 自动生成音乐的曲库目录 | 新机输出目录 |
| `H:\MusicAI\scripts\gen_song.py` | 52 行 `GEN_SONG` | 出曲脚本 | 新机路径 |
| `H:\h3cache` | 35 行 `OUT` | 交付归档根（原始/work/失败/films 都挂在下面） | 新机缓存盘根，如 `D:\h3cache` |
| `H:\h3cache\approved` | 3188 行 `approved_watcher` | 审核通过图目录（自动复制进 ComfyUI input） | 新机对应目录 |
| `H:\h3cache\bgm` | 54 行 `BGM_OUT` | 配乐落盘目录（曲库之一） | 新机对应目录 |
| `H:\h3cache\downloads` | 41 行 `DOWNLOADS` | 浏览器下载落点 | 新机对应目录 |
| `H:\h3cache\h3_cache` | 40 行 `CACHE` | 运行状态/日志/历史（pipeline_status.json、pipeline.log 等） | 新机对应目录 |
| `H:\h3cache\raw` | 34 行 `RAW` | 待处理视频输入队列目录（FIFO 扫描） | 新机对应目录 |

> 改法提示：以上常量集中在副本 34–77 行（字体表 127–134 行、whisper 模型 2108 行、approved 3188 行），一次性替换即可。

---

## 3. 端口与环境变量

### 端口

| 端口 | 用途 | 绑定 |
|---|---|---|
| **8898** | 本服务控制台/HTTP API（副本 57 行 `PORT`） | 仅 `127.0.0.1`（副本 3210 行） |
| 8188 | ComfyUI（上游依赖，非本服务） | 由 ComfyUI 决定 |
| 8001 | ACE-Step 音乐引擎（按需自动拉起） | 由音乐引擎决定 |

### 环境变量（均可选）

| 变量 | 默认 | 作用 |
|---|---|---|
| `HF_ENDPOINT` | 代码内 setdefault 为 `https://hf-mirror.com`（副本 32 行） | HuggingFace 镜像；已在环境里则尊重外部值 |
| `H3_PROMPT_SKILL` | 关 | 置 `1` 开启官方 h3-prompt-writing 提示词封装（`tools/h3_prompt_skill.py:213`；restart_pipeline.example.ps1 第 7 行会设置） |
| `H3_VOICE_AMBIENCE` | 关 | 置真值开启音色转换环境音保留（`tools/h3_voice.py:56`） |
| `H3_VOICE_AMBIENCE_BED_DB` / `H3_VOICE_AMBIENCE_VOICE_DB` | -3 / 3 | 环境音底噪/人声补偿 dB（`tools/h3_voice.py:58-59`） |
| `H3_GUARD_FFMPEG` | 无 | 守卫工具备用 ffmpeg 路径（`tools/h3_guard.py:98`） |
| `H3_GUARD_MODEL_DIR` | 内置默认 | 守卫工具备用模型目录（`tools/h3_guard.py:105`） |

---

## 4. 启动与停止

### 启动（后台无窗）

```powershell
# 方式一：直接起
& "D:\ComfyUI\venv\Scripts\pythonw.exe" "C:\H3DirectorDesk\video_pipeline.py"

# 方式二：用本目录 restart_pipeline.example.ps1（先按第 2 节改里面的两个路径）
powershell -NoProfile -ExecutionPolicy Bypass -File .\restart_pipeline.example.ps1
```

端口被占时新实例自动退出（副本 3211–3213 行，单实例保护），不会出现双实例。

### 停止

restart_pipeline.example.ps1 的做法（按命令行精确匹配，不误杀其他 python）：

```powershell
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*video_pipeline.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

### 健康检查

```bash
curl http://127.0.0.1:8898/status
```

返回 200 与 JSON（含任务/队列/事件流）即在线；控制台页面在 `http://127.0.0.1:8898/`。

---

## 5. 可外置配置说明

服务没有独立的配置文件机制（旧 `pipeline_config.json` 已废弃，副本 45 行仅保留常量）。可外置的配置分三类：

1. **`dialogue.json`**（全局台词/风格）：放程序目录；模板见本目录 **`config.example.json`**（含全部键说明）。按片覆盖：在 `raw` 目录放 `<视频名>.dialogue.json`（副本 1980–1987 行）。
2. **长片订单 `<片名>.film.json`**：放 `raw` 目录触发长片模式，键有 `style`/`lang`/`voice`(on/off)/`lines`/`reuse` 等（副本 2596–2612 行解析）。
3. **档案文件**：`tools/h3_guard_profiles/<名>.guard.json`（按片守卫）、`tools/h3_voice_profiles/`（音色参考），均为可选。

### 密钥脱敏记录

审计密钥清单为空；本代理另行用关键词扫描（api key / token / secret / password / Bearer / sk- 等）复核了 `video_pipeline.py` 与 `restart_pipeline.ps1`，均未命中。因此两份副本为**逐字节原样复制**（md5 一致），无脱敏占位符。

### 副本清单

| 文件 | 说明 |
|---|---|
| `video_pipeline.py` | 服务本体（与原件 md5 一致：`6accc9becf52c6b74a73e607113a7194`） |
| `h3_film_seg_template.json` | 长片模板（与原件 md5 一致：`8da080fa51d87454b34299ff74098617`）；部署时复制到程序目录 |
| `jev/` | JEV 三个粘合脚本（h3_judge / video_decide / storyboard_decide，与原件 md5 一致）；复制到 JEV 目录并改 `MODEL_DIR` 常量 |
| `tools/` | 守卫/音色/提示词技能脚本与示例档案；复制到程序目录 `tools\` |
| `restart_pipeline.example.ps1` | 重启脚本副本（与原件 md5 一致：`09651f3df6675ab7177784d86bb6f0ad`） |
| `config.example.json` | dialogue.json 模板 |
| `ADAPT.md` | 本指南 |
