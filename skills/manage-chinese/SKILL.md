---
name: manage-chinese
description: 管理 Antigravity GUI 简体中文汉化插件的状态、词典热更新、版本更新自愈及英文原版切换。当用户询问汉化状态、更新词典、版本更新后恢复汉化或还原界面时使用。
---

# Antigravity 界面汉化插件管理技能 (`manage-chinese`)

本技能用于管理已插件化的 `Antigravity` 2.0 简体中文本地化插件（`antigravity-zh-cn`）。本插件采用完全自包含结构，所有运行时、脚本与生命周期钩子均位于插件目录内。

## 运行机制

- 插件目录：`~/.gemini/config/plugins/antigravity-zh-cn/`
- 词典文件：`~/.gemini/config/plugins/antigravity-zh-cn/runtime/zh-CN.json`
- DOM 引擎：`~/.gemini/config/plugins/antigravity-zh-cn/runtime/i18n-engine.js`
- 自愈钩子：`~/.gemini/config/plugins/antigravity-zh-cn/hooks.json`（`SessionStart`）
- 宿主脚本：`~/.gemini/config/plugins/antigravity-zh-cn/scripts/plugin_host.py`
- 加载日志：`~/.gemini/config/plugins/antigravity-zh-cn/runtime/loader.log`
- 自愈日志：`~/.gemini/config/plugins/antigravity-zh-cn/runtime/heal.log`

`Electron` 主进程宿主加载器（`dist/pluginLoader.js`）会在页面加载及刷新时读取 `~/.gemini/config/config.json` 中插件的启用状态，仅在插件已启用时注入汉化。

## 操作指南

### 1. 检查汉化运行状态
执行插件内置脚本探测当前 `Antigravity` 安装目录并检查宿主状态：
```powershell
python "$env:USERPROFILE\.gemini\config\plugins\antigravity-zh-cn\scripts\plugin_host.py" status
```
也可查看 `~/.gemini/config/plugins/antigravity-zh-cn/runtime/loader.log` 与 `heal.log`，确认注入与自愈记录。

### 2. Antigravity 版本更新后的自动自愈与手动恢复
1. **自动恢复（`SessionStart` 钩子）**：当 `Antigravity` 自动更新覆盖 `app.asar` 后，在任意对话发起新会话时，`hooks.json` 会自动检测当前 `app.asar` 是否失去宿主加载器：
   - 先在不影响运行的前提下生成并校验 `app.asar.staged`（若新版结构不兼容则直接跳过并提示，绝不强行重启）；
   - 校验通过后，拉起独立后台恢复进程礼貌关闭 `Antigravity`、替换 `app.asar` 并自动重启恢复中文界面；
   - 同一版本 `app.asar` 具备防循环冷却保护（180 秒冷却 / 最多自动尝试 2 次）。
2. **手动接入**：如需立即手动执行，完全退出 `Antigravity` 后运行：
```powershell
python "$env:USERPROFILE\.gemini\config\plugins\antigravity-zh-cn\scripts\plugin_host.py" install
```

### 3. 热更新词典或翻译引擎
直接编辑 `~/.gemini/config/plugins/antigravity-zh-cn/runtime/zh-CN.json`。完成后只需在 `Antigravity` 窗口内按 `Ctrl+R` 刷新界面即可立即生效，**无需重新打包 `app.asar` 或运行任何脚本**。新增未收录的英文文案会自动保持原样英文，不影响已有翻译。

### 4. 切换回原版英文 / 卸载汉化
- **临时切换原版英文**：打开 `[antigravity-zh-cn](customization://antigravity-zh-cn)` 禁用插件后，按 `Ctrl+R` 刷新或重启即可恢复原版英文界面。
- **彻底还原 `app.asar`**：完全退出 `Antigravity` 后运行：
```powershell
python "$env:USERPROFILE\.gemini\config\plugins\antigravity-zh-cn\scripts\plugin_host.py" restore
```
