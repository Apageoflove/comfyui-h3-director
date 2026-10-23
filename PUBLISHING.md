# 发布到 GitHub 的步骤

本地已经是一个可直接推送的仓库（`git init` + 首次提交在插件迁入 H 盘后执行）。

## 1. 建仓库

```bash
gh repo create comfyui-h3-director --public \
  --description "ComfyUI integration for H3 Director's Desk: status, film submission and an embedded panel, zero impact on existing workflows."
```

或者网页新建空仓库，然后：

```bash
git remote add origin https://github.com/<你的账号>/comfyui-h3-director.git
git branch -M main
git push -u origin main
```

## 2. 仓库设置建议

- Topics：`comfyui` `comfyui-nodes` `h3` `minimax` `video-generation` `pipeline`
- 关闭 Wiki（文档都在 README）
- Release：`v0.1.0`，附 `comfyui-h3-director-v0.1.0.zip`（整个插件目录打包）

## 3. 上架 ComfyUI Registry（可选）

1. 在 https://registry.comfy.org 注册 publisher，拿到 `PublisherId`
2. 填进 `pyproject.toml` 的 `[tool.comfy] PublisherId`
3. 安装 `comfy-cli` 后执行：

```bash
comfy node publish
```

## 4. 发布前自查

- [ ] `python -m unittest discover -s tests -v` 全绿
- [ ] `config.json` 没有被提交（`.gitignore` 已忽略）
- [ ] README 里的截图存在且为真实截图
- [ ] 仓库不含任何模型权重、密钥、个人路径
- [ ] LICENSE 年份/署名确认
- [ ] 在干净的 ComfyUI 环境里装一次，确认只新增侧栏和 3 个节点，不影响原有工作流
