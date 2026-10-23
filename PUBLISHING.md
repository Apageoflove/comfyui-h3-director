# 发布记录与维护指南

## 当前状态（2026-10-23）

v0.1.0 已发布：

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/Apageoflove/comfyui-h3-director |
| 标签 | [`v0.1.0`](https://github.com/Apageoflove/comfyui-h3-director/releases/tag/v0.1.0) |
| 许可 | MIT |
| 仓库描述 | ComfyUI sidebar panel + 3 nodes for the H3 Director's Desk video pipeline, with a deployable service copy. |
| Topics（16） | comfyui · comfyui-nodes · comfyui-plugin · custom-nodes · minimax-h3 · minimax · h3 · video-generation · ai-video · video-pipeline · video-subtitles · faster-whisper · jev · automation · local-deployment · 8gb-vram |
| ComfyUI Registry | 未上架（步骤见下） |

## 后续版本发布流程（v0.1.x）

1. 本地仓库：`H:\h3cache\comfyui-h3-director`（H 盘为 exFAT，git 命令需带 `-c safe.directory='*'`）
2. 改动 → 单测：`python -m unittest discover -s tests -v`
3. 提交（用账号邮箱，贡献图才计绿格）：
   ```bash
   git -c safe.directory='*' -c user.name=Apageoflove -c user.email=zh13246570@gmail.com commit -am "..."
   git -c safe.directory='*' push origin main
   ```
4. 打标签发布：
   ```bash
   git -c safe.directory='*' tag v0.1.x
   git -c safe.directory='*' push origin v0.1.x
   ```
5. GitHub 网页 [releases/new](https://github.com/Apageoflove/comfyui-h3-director/releases/new) 选刚推的标签，填标题与说明，Publish（可选附 zip：右键插件目录打包，排除 `.git\` 与 `.token-optimizer\`）
6. 仓库描述 / Topics 修改：仓库页 About 齿轮，或 API（本机凭据已存于 Git Credential Manager）

## ComfyUI Registry 上架（可选）

1. 在 https://registry.comfy.org 注册 publisher，取得 `PublisherId`
2. 填进 `pyproject.toml` 的 `[tool.comfy] PublisherId`
3. `comfy node publish`

## 发布前自查（每次发版过一遍）

- [ ] `python -m unittest discover -s tests -v` 全绿
- [ ] `config.json` 未被提交（`.gitignore` 已忽略）
- [ ] README 截图引用与 `docs/screenshots/` 文件一一对应
- [ ] 无模型权重、密钥、个人路径混入（`git ls-files | grep -iE "key|token|secret"` 复查）
- [ ] 版本号三处同步：`version.py`、`pyproject.toml`、标签
- [ ] 在干净环境装一次，确认只新增侧栏与 3 个节点，不影响既有工作流
