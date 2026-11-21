# Antigravity 简体中文汉化插件 (`antigravity-zh-cn`)

为 Google `Antigravity` 2.0 桌面应用提供**自包含、跨小版本兼容、更新自动自愈**的简体中文本地化插件体验，覆盖主界面与系统托盘菜单，并内置专有名词保护与中文交互规则。

---

## 一句话安装提示词（交给 AI 自动安装）

如果你正在使用 `Antigravity`，直接复制下面这段提示词发送给 AI 助手即可完成安装：

```text
请帮我安装 GitHub 仓库 https://github.com/KiriuKazurei/antigravity-zh-cn-plugin 中的 Antigravity 简体中文汉化插件：将其克隆到用户插件目录 ~/.gemini/config/plugins/antigravity-zh-cn（Windows 为 %USERPROFILE%\.gemini\config\plugins\antigravity-zh-cn），运行其中的 scripts/plugin_host.py install 完成宿主接入并检查状态；若提示 app.asar 正在被占用，请引导我重启或通过脚本完成替换生效。
```

---

## 核心特性

1. **完全自包含插件结构**：词典、DOM 引擎、宿主加载器、`SessionStart` 自愈钩子、规则与管理 `Skill` 全部封装在单个插件目录中，部署至 `~/.gemini/config/plugins/antigravity-zh-cn/` 即可使用。
2. **全界面 + 系统托盘汉化**：
   - 前端通过 DOM `MutationObserver` 与精确/正则词典（`runtime/zh-CN.json`）实时翻译界面文本，不依赖前端构建哈希或行号。
   - 宿主加载器（`scripts/pluginLoader.js`）支持系统托盘菜单汉化，且采用非致命降级策略，即使后续版本微调托盘实现也不会影响主界面汉化。
3. **连续兼容多个 `Antigravity` 小版本**：已收录的旧有界面文案自动汉化，新版新增文案自动保持原样英文，直到词典更新适配，不会导致界面报错或空白。
4. **版本更新后自动自愈（基于 `hooks.json`）**：
   - `Antigravity` 自动更新覆盖 `resources/app.asar` 后，下次开启任意新对话时，`SessionStart` 钩子会自动检测并预编译校验 `app.asar.staged`。
   - 校验通过后自动礼貌关闭 `Antigravity`、替换 `app.asar` 并自动重启恢复中文（内置同版本防死循环冷却与次数保护）；若预编译校验失败则会安全跳过重启，绝不干扰正常使用。
5. **词典热更新与一键切换英文**：
   - 修改 `runtime/zh-CN.json` 后，在 `Antigravity` 窗口内按 `Ctrl+R` 即可即时生效，无需重新打包 `app.asar`。
   - 在 `Antigravity` 插件设置中禁用 `antigravity-zh-cn` 并按 `Ctrl+R`，即可立即切换回原版英文界面。
6. **专有名词保护与内置管理 `Skill`**：
   - 内置 `rules/AGENTS.md`，严格保留 `Gemini`、`Antigravity`、`MCP`、`Artifact`、`Subagent`、`Sidecar`、`Skill`、`Git`、`Python`、`/plan` 等英文专有名词与斜杠指令。
   - 内置 `skills/manage-chinese/SKILL.md`，可在对话中直接协助检查汉化状态、补充词典或恢复原版。

---

## 手动安装与使用

### 环境要求

- `Python 3`
- `Git`（可选，也可直接下载仓库 ZIP 解压）

### 1. 获取并安装插件

#### Windows (PowerShell)

```powershell
# 1. 克隆到 Antigravity 插件目录
git clone https://github.com/KiriuKazurei/antigravity-zh-cn-plugin.git "$env:USERPROFILE\.gemini\config\plugins\antigravity-zh-cn"

# 2. 完全退出 Antigravity 后执行一次宿主接入
python "$env:USERPROFILE\.gemini\config\plugins\antigravity-zh-cn\scripts\plugin_host.py" install
```

#### macOS / Linux (Bash / Zsh)

```bash
# 1. 克隆到 Antigravity 插件目录
git clone https://github.com/KiriuKazurei/antigravity-zh-cn-plugin.git ~/.gemini/config/plugins/antigravity-zh-cn

# 2. 完全退出 Antigravity 后执行一次宿主接入
python3 ~/.gemini/config/plugins/antigravity-zh-cn/scripts/plugin_host.py install
```

> [!TIP]
> 也可以先将本仓库克隆到任意本地目录，再执行 `python scripts/plugin_host.py install`，脚本会自动将插件同步到 `~/.gemini/config/plugins/antigravity-zh-cn/` 并完成宿主接入。
> 之后当 `Antigravity` 小版本自动更新时，`hooks.json` 中的 `SessionStart` 钩子会在开启新对话时自动完成自愈恢复，无需再次手动运行脚本。

### 2. 常用管理命令

Windows (PowerShell)：

```powershell
# 查看插件、宿主版本、备份与自愈状态
python "$env:USERPROFILE\.gemini\config\plugins\antigravity-zh-cn\scripts\plugin_host.py" status

# 仅预编译生成 app.asar.staged，不立即替换
python "$env:USERPROFILE\.gemini\config\plugins\antigravity-zh-cn\scripts\plugin_host.py" stage

# 彻底还原官方原版 app.asar（需先完全退出 Antigravity）
python "$env:USERPROFILE\.gemini\config\plugins\antigravity-zh-cn\scripts\plugin_host.py" restore
```

macOS / Linux 将路径替换为 `~/.gemini/config/plugins/antigravity-zh-cn/scripts/plugin_host.py`（使用 `python3`）即可。

### 3. 运行词典与引擎单元测试

```bash
node runtime/test_i18n.js
```

---

## 目录结构

```text
antigravity-zh-cn/
├── plugin.json                   # 插件清单
├── hooks.json                    # SessionStart 更新自愈钩子
├── README.md                     # 说明文档
├── LICENSE                       # MIT 开源协议
├── rules/
│   └── AGENTS.md                 # 中文交互与专有名词保护规则
├── skills/
│   └── manage-chinese/
│       └── SKILL.md              # 汉化插件管理技能
├── scripts/
│   ├── plugin_host.py            # 自包含宿主安装、校验与自动自愈脚本
│   ├── session_hook.py           # SessionStart 钩子安全入口
│   └── pluginLoader.js           # 注入 app.asar 的通用插件宿主加载器
└── runtime/
    ├── i18n-engine.js            # DOM 汉化引擎
    ├── zh-CN.json                # 简体中文词典（支持 Ctrl+R 热更新）
    └── test_i18n.js              # 词典与引擎单元测试
```

---

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
