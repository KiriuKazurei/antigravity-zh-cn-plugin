"""
Antigravity 简体中文插件自包含宿主管理与更新自愈工具
Self-contained host manager for antigravity-zh-cn:
- Cross-minor-version resilient app.asar patching (main.js regex fallback + non-fatal tray.js)
- SessionStart hook verification & pre-staged auto-healing (polite close -> patch -> relaunch)
- Safe anti-loop guard and backup verification
"""

import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(SCRIPT_DIR)
RUNTIME_DIR = os.path.join(PLUGIN_DIR, "runtime")


def _resolve_plugin_name():
    try:
        manifest_path = os.path.join(PLUGIN_DIR, "plugin.json")
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            name = data.get("name") if isinstance(data, dict) else None
            if isinstance(name, str) and name.strip():
                return name.strip()
    except Exception:
        pass
    return os.path.basename(PLUGIN_DIR)


PLUGIN_NAME = _resolve_plugin_name()
PLUGIN_LOADER_SRC = os.path.join(SCRIPT_DIR, "pluginLoader.js")
HEAL_STATE_PATH = os.path.join(RUNTIME_DIR, "heal_state.json")
HEAL_LOG_PATH = os.path.join(RUNTIME_DIR, "heal.log")
PLUGINS_ROOT = os.path.join(os.path.expanduser("~"), ".gemini", "config", "plugins")

HEAL_COOLDOWN_SEC = 180
MAX_HEAL_ATTEMPTS_PER_HASH = 2


def strip_json_comments(text):
    out = []
    in_string = False
    escaped = False
    in_line_comment = False
    in_block_comment = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
        elif ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
        else:
            out.append(ch)
            i += 1
    cleaned = "".join(out)
    return re.sub(r",\s*([}\]])", r"\1", cleaned)


def read_json_safe(file_path, is_jsonc=False):
    try:
        if not os.path.exists(file_path):
            return None
        with open(file_path, "r", encoding="utf-8") as f:
            raw = f.read()
        return json.loads(strip_json_comments(raw) if is_jsonc else raw)
    except Exception:
        return None


def write_heal_log(message):
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        if os.path.exists(HEAL_LOG_PATH) and os.path.getsize(HEAL_LOG_PATH) > 65536:
            with open(HEAL_LOG_PATH, "w", encoding="utf-8") as f:
                f.write("")
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(HEAL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def is_plugin_enabled():
    manifest = read_json_safe(os.path.join(PLUGIN_DIR, "plugin.json"), is_jsonc=True) or {}
    default_enabled = manifest.get("disabled") is not True
    config_path = os.path.join(os.path.expanduser("~"), ".gemini", "config", "config.json")
    config = read_json_safe(config_path, is_jsonc=False)
    if isinstance(config, dict):
        plugins_cfg = config.get("plugins")
        if isinstance(plugins_cfg, dict):
            entry = plugins_cfg.get(PLUGIN_NAME) or plugins_cfg.get(manifest.get("name", ""))
            if isinstance(entry, dict) and isinstance(entry.get("enabled"), bool):
                return entry["enabled"]
    return default_enabled


def find_resources_dir(explicit_dir=None):
    candidates = []
    if explicit_dir:
        candidates.append(explicit_dir)
    env_dir = os.environ.get("ANTIGRAVITY_RESOURCES_DIR")
    if env_dir:
        candidates.append(env_dir)

    system = platform.system()
    if system == "Windows":
        try:
            out = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-Process -Name Antigravity -ErrorAction SilentlyContinue | Select-Object -First 1).Path",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            ).strip()
            if out and os.path.exists(out):
                candidates.append(os.path.join(os.path.dirname(out), "resources"))
        except Exception:
            pass

        local_app_data = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("ProgramFiles", "")
        if local_app_data:
            candidates.append(os.path.join(local_app_data, "Programs", "antigravity", "resources"))
            candidates.append(os.path.join(local_app_data, "Programs", "Antigravity", "resources"))
        if program_files:
            candidates.append(os.path.join(program_files, "Antigravity", "resources"))
        candidates.append(r"K:\AI\Antigravity\resources")
    elif system == "Darwin":
        candidates.extend(
            [
                "/Applications/Antigravity.app/Contents/Resources",
                os.path.expanduser("~/Applications/Antigravity.app/Contents/Resources"),
            ]
        )
    else:
        candidates.extend(
            [
                "/opt/Antigravity/resources",
                "/usr/lib/antigravity/resources",
                os.path.expanduser("~/.local/share/antigravity/resources"),
            ]
        )

    for d in candidates:
        if d and os.path.exists(os.path.join(d, "app.asar")):
            return d
    return None


def find_antigravity_exe(resources_dir):
    if not resources_dir:
        return None
    system = platform.system()
    parent = os.path.dirname(os.path.abspath(resources_dir))
    if system == "Windows":
        exe = os.path.join(parent, "Antigravity.exe")
        return exe if os.path.exists(exe) else None
    if system == "Darwin":
        exe = os.path.join(parent, "MacOS", "Antigravity")
        return exe if os.path.exists(exe) else None
    for name in ("antigravity", "Antigravity"):
        exe = os.path.join(parent, name)
        if os.path.exists(exe):
            return exe
    return None


def file_sha256(file_path):
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def read_asar_header(asar_path):
    with open(asar_path, "rb") as f:
        _, _, _, header_str_size = struct.unpack("<IIII", f.read(16))
        header = json.loads(f.read(header_str_size).decode("utf-8"))
        base_offset = 16 + ((header_str_size + 3) & ~3)
    return header, base_offset


def read_asar_file(asar_path, parts):
    header, base_offset = read_asar_header(asar_path)
    node = header
    for part in parts:
        node = node.get("files", {}).get(part)
        if not isinstance(node, dict):
            return None
    if node.get("unpacked"):
        return None
    with open(asar_path, "rb") as f:
        f.seek(base_offset + int(node["offset"]))
        return f.read(int(node["size"]))


def load_asar(asar_path):
    with open(asar_path, "rb") as f:
        magic, header_size, header_obj_size, header_str_size = struct.unpack(
            "<IIII", f.read(16)
        )
        header = json.loads(f.read(header_str_size).decode("utf-8"))
        base_offset = 16 + ((header_str_size + 3) & ~3)
        file_bytes = {}

        def read_tree(node, parts):
            for name, meta in node.get("files", {}).items():
                cur = parts + [name]
                if "files" in meta:
                    read_tree(meta, cur)
                elif not meta.get("unpacked"):
                    f.seek(base_offset + int(meta["offset"]))
                    file_bytes[tuple(cur)] = f.read(int(meta["size"]))

        read_tree(header, [])
    return header, file_bytes


def write_asar(dst_path, header, file_bytes):
    cur_offset = 0

    def update_tree(node, parts):
        nonlocal cur_offset
        for name, meta in node.get("files", {}).items():
            cur = parts + [name]
            if "files" in meta:
                update_tree(meta, cur)
            elif not meta.get("unpacked"):
                data = file_bytes[tuple(cur)]
                meta["size"] = len(data)
                meta["offset"] = str(cur_offset)
                cur_offset += len(data)

    update_tree(header, [])

    header_raw = json.dumps(header, separators=(",", ":")).encode("utf-8")
    header_str_size = len(header_raw)
    padding = (4 - (header_str_size % 4)) % 4
    padded_header = header_raw + (b"\x00" * padding)

    header_obj_size = len(padded_header) + 4
    header_size = header_obj_size + 4
    magic = 4

    tmp_path = dst_path + ".tmp"
    with open(tmp_path, "wb") as out_f:
        out_f.write(
            struct.pack("<IIII", magic, header_size, header_obj_size, header_str_size)
        )
        out_f.write(padded_header)

        def write_tree(node, parts):
            for name, meta in node.get("files", {}).items():
                cur = parts + [name]
                if "files" in meta:
                    write_tree(meta, cur)
                elif not meta.get("unpacked"):
                    out_f.write(file_bytes[tuple(cur)])

        write_tree(header, [])

    if os.path.exists(dst_path):
        os.remove(dst_path)
    os.rename(tmp_path, dst_path)


def get_asar_version(asar_path):
    try:
        raw = read_asar_file(asar_path, ("package.json",))
        pkg = json.loads((raw or b"{}").decode("utf-8"))
        return pkg.get("version")
    except Exception:
        return None


def has_plugin_loader(asar_path):
    try:
        header, _ = read_asar_header(asar_path)
        dist_files = header.get("files", {}).get("dist", {}).get("files", {})
        return "pluginLoader.js" in dist_files
    except Exception:
        return False


def has_current_plugin_loader(asar_path):
    try:
        if not os.path.exists(PLUGIN_LOADER_SRC):
            return False
        installed = read_asar_file(asar_path, ("dist", "pluginLoader.js"))
        if installed is None:
            return False
        with open(PLUGIN_LOADER_SRC, "rb") as f:
            expected = f.read()
        return installed == expected
    except Exception:
        return False


def has_legacy_preload_patch(asar_path):
    try:
        raw = read_asar_file(asar_path, ("dist", "preload.js"))
        preload = (raw or b"").decode("utf-8", errors="ignore")
        return (
            "__ANTIGRAVITY_I18N_LOADED__" in preload
            or "Antigravity Chinese Localization Hook" in preload
        )
    except Exception:
        return False


def patch_main_js(main_js):
    if 'require("./pluginLoader")' in main_js:
        return main_js

    # 1. Inject pluginLoader require cleanly (exact -> regex -> module header fallback)
    loader_req = 'const pluginLoader_1 = require("./pluginLoader");'
    exact_import = 'const tray_1 = require("./tray");'
    if exact_import in main_js:
        main_js = main_js.replace(exact_import, exact_import + "\n" + loader_req, 1)
    else:
        m_req = re.search(r'(const\s+\w+\s*=\s*require\(["\']\./tray["\']\);)', main_js)
        if m_req:
            main_js = main_js.replace(m_req.group(1), m_req.group(1) + "\n" + loader_req, 1)
        else:
            m_es = re.search(r'(Object\.defineProperty\(exports,\s*["\']__esModule["\'][^;]+;\s*)', main_js)
            if m_es:
                main_js = main_js.replace(m_es.group(1), m_es.group(1) + loader_req + "\n", 1)
            else:
                main_js = loader_req + "\n" + main_js

    # 2. Inject web-contents-created listener (exact -> regex fallback)
    has_script_jail = "scriptJail_1" in main_js and "isScriptJailSession" in main_js
    exact_wc = "electron_1.app.on('web-contents-created', (_event, contents) => {"
    if exact_wc in main_js:
        guard = (
            "if (!(0, scriptJail_1.isScriptJailSession)(contents.session)) {\n"
            "        (0, pluginLoader_1.attachWebContents)(contents);\n    }"
            if has_script_jail
            else "(0, pluginLoader_1.attachWebContents)(contents);"
        )
        main_js = main_js.replace(exact_wc, exact_wc + "\n    " + guard, 1)
    else:
        m_wc = re.search(
            r"(\w+\.app\.on\(\s*['\"]web-contents-created['\"]\s*,\s*\(\s*[^,]+,\s*(\w+)\s*\)\s*=>\s*\{)",
            main_js,
        )
        if not m_wc:
            raise RuntimeError("未能在 dist/main.js 中找到 web-contents-created 锚点")
        contents_var = m_wc.group(2)
        guard = (
            f"if (!(0, scriptJail_1.isScriptJailSession)({contents_var}.session)) {{\n"
            f"        (0, pluginLoader_1.attachWebContents)({contents_var});\n    }}"
            if has_script_jail
            else f"(0, pluginLoader_1.attachWebContents)({contents_var});"
        )
        main_js = main_js.replace(m_wc.group(1), m_wc.group(1) + "\n    " + guard, 1)

    return main_js


def patch_tray_js(tray_js):
    """Best-effort tray localization hook; never aborts main UI patching on minor refactors."""
    warnings = []
    if 'require("./pluginLoader")' in tray_js:
        return tray_js, warnings

    loader_req = 'const pluginLoader_1 = require("./pluginLoader");'
    exact_imp = 'const utils_1 = require("./utils");'
    if exact_imp in tray_js:
        tray_js = tray_js.replace(exact_imp, exact_imp + "\n" + loader_req, 1)
    else:
        m_imp = re.search(r'(const\s+\w+\s*=\s*require\(["\']\./utils["\']\);)', tray_js)
        if m_imp:
            tray_js = tray_js.replace(m_imp.group(1), m_imp.group(1) + "\n" + loader_req, 1)
        else:
            warnings.append("dist/tray.js 未找到 utils 导入锚点，已跳过托盘汉化")
            return tray_js, warnings

    # Hook Menu.buildFromTemplate(actions)
    if "contextMenu = electron_1.Menu.buildFromTemplate(actions);" in tray_js:
        tray_js = tray_js.replace(
            "contextMenu = electron_1.Menu.buildFromTemplate(actions);",
            "contextMenu = electron_1.Menu.buildFromTemplate((0, pluginLoader_1.translateTrayItems)(actions));",
            1,
        )
    else:
        m_tpl = re.subn(
            r"(\w+\.Menu\.buildFromTemplate\()(\w+)(\))",
            r"\1(0, pluginLoader_1.translateTrayItems)(\2)\3",
            tray_js,
            count=1,
        )
        if m_tpl[1] > 0:
            tray_js = m_tpl[0]
        else:
            warnings.append("dist/tray.js 未找到 Menu.buildFromTemplate 锚点")

    # Hook new MenuItem(options)
    if "new electron_1.MenuItem(options)" in tray_js:
        tray_js = tray_js.replace(
            "new electron_1.MenuItem(options)",
            "new electron_1.MenuItem((0, pluginLoader_1.translateTrayItem)(options))",
            1,
        )
    else:
        m_mi = re.subn(
            r"(new\s+\w+\.MenuItem\()(\w+)(\))",
            r"\1(0, pluginLoader_1.translateTrayItem)(\2)\3",
            tray_js,
            count=1,
        )
        if m_mi[1] > 0:
            tray_js = m_mi[0]
        else:
            warnings.append("dist/tray.js 未找到 MenuItem 锚点")

    # Hook agent count formatting
    old_count_block = """            countItem.label =
                (count > 0 ? `${count}` : 'No') +
                    ' agent' +
                    (count === 1 ? '' : 's') +
                    ' running';"""
    new_count_block = """            const translatedCount = (0, pluginLoader_1.formatTrayAgentCount)(count);
            countItem.label =
                translatedCount ??
                    ((count > 0 ? `${count}` : 'No') +
                        ' agent' +
                        (count === 1 ? '' : 's') +
                        ' running');"""
    if old_count_block in tray_js:
        tray_js = tray_js.replace(old_count_block, new_count_block, 1)
    else:
        m_cnt = re.search(
            r"countItem\.label\s*=\s*(\(count\s*>\s*0\s*\?\s*`\$\{count\}`\s*:\s*'No'\)\s*\+\s*' agent'\s*\+\s*\(count\s*===\s*1\s*\?\s*''\s*:\s*'s'\)\s*\+\s*' running');",
            tray_js,
        )
        if m_cnt:
            expr = m_cnt.group(1).rstrip(";")
            tray_js = tray_js.replace(
                m_cnt.group(0),
                f"const translatedCount = (0, pluginLoader_1.formatTrayAgentCount)(count);\n            countItem.label = translatedCount ?? ({expr});",
                1,
            )
        else:
            warnings.append("dist/tray.js 未找到 updateTrayAgentCount 锚点")

    return tray_js, warnings


def build_patched_asar(src_asar, dst_asar, quiet=False):
    header, file_bytes = load_asar(src_asar)

    with open(PLUGIN_LOADER_SRC, "rb") as f:
        loader_bytes = f.read()
    file_bytes[("dist", "pluginLoader.js")] = loader_bytes
    header["files"]["dist"]["files"]["pluginLoader.js"] = {
        "size": len(loader_bytes),
        "offset": "0",
    }

    main_js = file_bytes[("dist", "main.js")].decode("utf-8")
    file_bytes[("dist", "main.js")] = patch_main_js(main_js).encode("utf-8")

    if ("dist", "tray.js") in file_bytes:
        tray_js = file_bytes[("dist", "tray.js")].decode("utf-8")
        patched_tray, warnings = patch_tray_js(tray_js)
        file_bytes[("dist", "tray.js")] = patched_tray.encode("utf-8")
        for w in warnings:
            if not quiet:
                print(f"[警告] {w}")
            write_heal_log(f"WARN: {w}")

    write_asar(dst_asar, header, file_bytes)

    # Verify the generated archive can be parsed and contains the loader
    _, verify_bytes = load_asar(dst_asar)
    if ("dist", "pluginLoader.js") not in verify_bytes:
        raise RuntimeError("校验失败: 生成的 asar 中缺少 dist/pluginLoader.js")
    if 'require("./pluginLoader")' not in verify_bytes[("dist", "main.js")].decode("utf-8"):
        raise RuntimeError("校验失败: 生成的 asar 中 dist/main.js 未包含 pluginLoader 引用")


def get_paths(explicit_dir=None):
    res_dir = find_resources_dir(explicit_dir)
    if not res_dir:
        return None, None, None, None, None
    return (
        res_dir,
        os.path.join(res_dir, "app.asar"),
        os.path.join(res_dir, "app.asar.bak"),
        os.path.join(res_dir, "app.asar.staged"),
        os.path.join(res_dir, "i18n"),
    )


def ensure_clean_backup(asar_path, bak_path, quiet=False):
    cur_ver = get_asar_version(asar_path)
    is_clean_official = not has_plugin_loader(asar_path) and not has_legacy_preload_patch(asar_path)
    if is_clean_official:
        shutil.copy2(asar_path, bak_path)
        if not quiet:
            print(f"已基于当前官方原版 (v{cur_ver}) 更新备份: {bak_path}")
        return
    if not os.path.exists(bak_path):
        raise RuntimeError("找不到原版备份 app.asar.bak，且当前 app.asar 已被修改过。")
    bak_ver = get_asar_version(bak_path)
    if cur_ver and bak_ver and cur_ver != bak_ver:
        raise RuntimeError(
            f"备份版本 (v{bak_ver}) 与当前 app.asar 版本 (v{cur_ver}) 不匹配，已中止以防降级损坏。"
        )


def sync_to_plugins_dir():
    dst_dir = os.path.join(PLUGINS_ROOT, PLUGIN_NAME)
    if os.path.abspath(PLUGIN_DIR) == os.path.abspath(dst_dir):
        return dst_dir
    os.makedirs(dst_dir, exist_ok=True)
    preserve = {"loader.log", "heal.log", "heal_state.json"}
    for root, dirs, files in os.walk(PLUGIN_DIR):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        rel_root = os.path.relpath(root, PLUGIN_DIR)
        dst_root = dst_dir if rel_root == "." else os.path.join(dst_dir, rel_root)
        os.makedirs(dst_root, exist_ok=True)
        for fn in files:
            if fn.endswith(".pyc"):
                continue
            if rel_root == "runtime" and fn in preserve and os.path.exists(os.path.join(dst_root, fn)):
                continue
            shutil.copy2(os.path.join(root, fn), os.path.join(dst_root, fn))
    print(f"已同步插件文件至: {dst_dir}")
    return dst_dir


def cmd_status(explicit_dir=None):
    res_dir, asar_path, bak_path, staged_path, _ = get_paths(explicit_dir)
    print("=== Antigravity 汉化插件状态检查 ===")
    print(f"- 插件目录: {PLUGIN_DIR}")
    print(f"- 插件启用: {is_plugin_enabled()}")
    print(
        f"- 运行时就绪: {os.path.exists(os.path.join(RUNTIME_DIR, 'zh-CN.json')) and os.path.exists(os.path.join(RUNTIME_DIR, 'i18n-engine.js'))}"
    )
    print(f"- 探测到安装目录: {res_dir or '未找到'}")
    if res_dir:
        print(f"- app.asar 存在: {os.path.exists(asar_path)} (版本: {get_asar_version(asar_path)})")
        print(
            f"- app.asar.bak 备份存在: {os.path.exists(bak_path)}"
            + (f" (版本: {get_asar_version(bak_path)})" if os.path.exists(bak_path) else "")
        )
        print(f"- app.asar 已安装宿主加载器: {has_plugin_loader(asar_path)}")
        print(f"- app.asar 宿主加载器为最新: {has_current_plugin_loader(asar_path)}")
        print(f"- app.asar 存在旧版失效注入: {has_legacy_preload_patch(asar_path)}")
        print(f"- app.asar.staged 待应用包存在: {os.path.exists(staged_path)}")
    print(f"- 宿主注入日志存在: {os.path.exists(os.path.join(RUNTIME_DIR, 'loader.log'))}")
    print(f"- 自动自愈日志存在: {os.path.exists(HEAL_LOG_PATH)}")


def cmd_install(stage_only=False, explicit_dir=None, do_sync=True):
    if do_sync:
        sync_to_plugins_dir()

    res_dir, asar_path, bak_path, staged_path, legacy_dir = get_paths(explicit_dir)
    if not res_dir:
        print("错误: 未找到 Antigravity 安装目录 (请用 --resources-dir 指定)")
        sys.exit(1)

    ensure_clean_backup(asar_path, bak_path)

    if os.path.exists(legacy_dir):
        shutil.rmtree(legacy_dir, ignore_errors=True)
        print(f"已清理旧版外置目录: {legacy_dir}")

    build_patched_asar(bak_path, staged_path)
    print(f"已从原版备份生成插件宿主包 (v{get_asar_version(bak_path)}): {staged_path}")

    if stage_only:
        return

    try:
        if os.path.exists(asar_path):
            os.remove(asar_path)
        os.rename(staged_path, asar_path)
        print("[成功] 宿主加载器已直接安装至 app.asar，启动 Antigravity 即可生效！")
    except PermissionError:
        print("[就绪] app.asar 当前正被运行中的 Antigravity 占用，已生成 app.asar.staged 等待替换。")


def cmd_restore(explicit_dir=None):
    res_dir, asar_path, bak_path, staged_path, _ = get_paths(explicit_dir)
    if not res_dir or not os.path.exists(bak_path):
        print("错误: 找不到原版备份 app.asar.bak")
        sys.exit(1)
    try:
        shutil.copy2(bak_path, asar_path)
        if os.path.exists(staged_path):
            os.remove(staged_path)
        print("[成功] 已从 app.asar.bak 恢复原版 app.asar。")
    except PermissionError:
        print("错误: app.asar 正在被运行中的 Antigravity 占用，请先完全退出 Antigravity 再重试。")
        sys.exit(1)


def load_heal_state():
    return read_json_safe(HEAL_STATE_PATH, is_jsonc=False) or {}


def save_heal_state(state):
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        with open(HEAL_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def spawn_detached_healer(resources_dir, exe_path):
    cmd_args = [
        sys.executable,
        os.path.abspath(__file__),
        "apply-and-restart",
        "--resources-dir",
        resources_dir,
    ]
    if exe_path:
        cmd_args.extend(["--exe", exe_path])

    if platform.system() == "Windows":
        # Double-fork on Windows breaks the parent-child PID link to language_server.exe
        # so `taskkill /pid <ls_pid> /T /F` during Antigravity shutdown will not kill the healer.
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        launcher_code = (
            "import subprocess; "
            f"subprocess.Popen({cmd_args!r}, "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
            f"close_fds=True, creationflags={DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP})"
        )
        mid = subprocess.Popen(
            [sys.executable, "-c", launcher_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        )
        mid.wait(timeout=3)
    else:
        subprocess.Popen(
            cmd_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )


def is_antigravity_running():
    system = platform.system()
    try:
        if system == "Windows":
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq Antigravity.exe", "/NH"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return "Antigravity.exe" in out
        if system == "Darwin":
            rc = subprocess.run(["pgrep", "-x", "Antigravity"], capture_output=True).returncode
            return rc == 0
        rc = subprocess.run(["pgrep", "-f", "antigravity"], capture_output=True).returncode
        return rc == 0
    except Exception:
        return False


def polite_close_antigravity():
    system = platform.system()
    if system == "Windows":
        # 1. Polite WM_CLOSE first (saves window bounds and workspace state cleanly)
        write_heal_log("Sending polite close (WM_CLOSE) to Antigravity.exe...")
        subprocess.run(
            ["taskkill", "/IM", "Antigravity.exe"],
            capture_output=True,
            timeout=5,
        )
        for _ in range(12):  # Wait up to 6s
            time.sleep(0.5)
            if not is_antigravity_running():
                write_heal_log("Antigravity.exe exited gracefully.")
                return
        # 2. Fallback if RUN_IN_BACKGROUND kept tray process alive
        write_heal_log("Antigravity.exe still running (likely tray background mode); closing tree...")
        subprocess.run(
            ["taskkill", "/IM", "Antigravity.exe", "/F", "/T"],
            capture_output=True,
            timeout=5,
        )
        for _ in range(10):
            time.sleep(0.3)
            if not is_antigravity_running():
                break
    elif system == "Darwin":
        subprocess.run(
            ["osascript", "-e", 'tell application "Antigravity" to quit'],
            capture_output=True,
            timeout=5,
        )
        for _ in range(12):
            time.sleep(0.5)
            if not is_antigravity_running():
                return
        subprocess.run(["pkill", "-TERM", "-x", "Antigravity"], capture_output=True)
    else:
        subprocess.run(["pkill", "-TERM", "-f", "Antigravity"], capture_output=True)
        for _ in range(12):
            time.sleep(0.5)
            if not is_antigravity_running():
                return


def relaunch_antigravity(exe_path):
    if not exe_path or not os.path.exists(exe_path):
        write_heal_log(f"Cannot relaunch: executable not found ({exe_path})")
        return
    write_heal_log(f"Relaunching Antigravity: {exe_path}")
    if platform.system() == "Windows":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            [exe_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        )
    elif platform.system() == "Darwin":
        app_bundle = os.path.abspath(os.path.join(os.path.dirname(exe_path), "..", ".."))
        subprocess.Popen(["open", app_bundle], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(
            [exe_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )


def cmd_apply_and_restart(resources_dir, exe_path=None):
    write_heal_log(f"Healer started (resources_dir={resources_dir}, exe={exe_path})")
    asar_path = os.path.join(resources_dir, "app.asar")
    staged_path = os.path.join(resources_dir, "app.asar.staged")
    if not os.path.exists(staged_path):
        write_heal_log("Aborted: app.asar.staged missing.")
        return

    # Give SessionStart hook enough time to exit cleanly and render the notice
    time.sleep(1.8)

    polite_close_antigravity()

    # Wait briefly for Windows file locks to drain, then swap atomically
    replaced = False
    last_err = None
    for _ in range(25):
        try:
            if os.path.exists(asar_path):
                os.remove(asar_path)
            os.rename(staged_path, asar_path)
            replaced = True
            break
        except PermissionError as exc:
            last_err = exc
            time.sleep(0.3)
        except Exception as exc:
            last_err = exc
            break

    if not replaced:
        write_heal_log(f"Failed to replace app.asar: {last_err}")
        return

    ver = get_asar_version(asar_path)
    write_heal_log(f"Successfully patched app.asar (v{ver}).")
    state = load_heal_state()
    state["last_success_ts"] = int(time.time())
    state["last_success_version"] = ver
    save_heal_state(state)

    if not exe_path:
        exe_path = find_antigravity_exe(resources_dir)
    relaunch_antigravity(exe_path)


def make_user_notice(markdown):
    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": "",
                    "metadata": {"user_markdown": markdown},
                }
            }
        ]
    }


def run_session_hook():
    # Consume stdin payload cleanly without hanging
    try:
        if not sys.stdin.isatty():
            _ = sys.stdin.read()
    except Exception:
        pass

    if not is_plugin_enabled():
        return {}

    res_dir, asar_path, bak_path, staged_path, _ = get_paths()
    if not res_dir or not os.path.exists(asar_path):
        return {}

    # Fast path: loader is already present and up to date in active app.asar
    has_loader = has_plugin_loader(asar_path)
    if has_loader and has_current_plugin_loader(asar_path):
        return {}

    cur_ver = get_asar_version(asar_path) or "unknown"
    if has_legacy_preload_patch(asar_path):
        return make_user_notice(
            f"检测到旧版汉化残留，请运行 `python \"{os.path.join(SCRIPT_DIR, 'plugin_host.py')}\" restore` 后再执行 `install`。"
        )

    asar_hash = file_sha256(asar_path)
    loader_hash = file_sha256(PLUGIN_LOADER_SRC) if os.path.exists(PLUGIN_LOADER_SRC) else ""
    heal_key = f"{asar_hash}:{loader_hash}"
    now = int(time.time())
    state = load_heal_state()
    prev_key = state.get("heal_key") or state.get("asar_sha256")
    attempts = state.get("attempts", 0) if prev_key == heal_key else 0
    last_attempt = state.get("last_attempt_ts", 0) if prev_key == heal_key else 0

    if attempts >= MAX_HEAL_ATTEMPTS_PER_HASH or (last_attempt and now - last_attempt < HEAL_COOLDOWN_SEC):
        write_heal_log(f"Skipped auto-heal for v{cur_ver} ({heal_key[:20]}): attempts={attempts}, cooldown")
        return make_user_notice(
            f"检测到 **Antigravity**（`v{cur_ver}`）尚未接入最新插件宿主（已触发防循环保护）。如需手动修复，请完全退出后运行：`python \"{os.path.join(SCRIPT_DIR, 'plugin_host.py')}\" install`"
        )

    # Pre-stage & verify BEFORE touching running Antigravity
    try:
        ensure_clean_backup(asar_path, bak_path, quiet=True)
        build_patched_asar(bak_path, staged_path, quiet=True)
    except Exception as exc:
        write_heal_log(f"Pre-stage failed for v{cur_ver}: {exc}")
        state.update(
            {
                "heal_key": heal_key,
                "asar_sha256": asar_hash,
                "version": cur_ver,
                "attempts": attempts + 1,
                "last_attempt_ts": now,
                "last_error": str(exc),
            }
        )
        save_heal_state(state)
        return make_user_notice(
            f"检测到 **Antigravity**（`v{cur_ver}`）需要更新插件宿主，但预构建失败（已安全跳过重启，不影响正常使用）：`{exc}`"
        )

    state.update(
        {
            "heal_key": heal_key,
            "asar_sha256": asar_hash,
            "version": cur_ver,
            "attempts": attempts + 1,
            "last_attempt_ts": now,
            "last_error": "",
        }
    )
    save_heal_state(state)

    exe_path = find_antigravity_exe(res_dir)
    try:
        spawn_detached_healer(res_dir, exe_path)
    except Exception as exc:
        write_heal_log(f"Failed to spawn detached healer: {exc}")
        return make_user_notice(
            f"检测到 **Antigravity**（`v{cur_ver}`）插件宿主后台重启拉起失败（`{exc}`）。已预生成补丁包，退出软件后运行 `python \"{os.path.join(SCRIPT_DIR, 'plugin_host.py')}\" install` 即可。"
        )

    reason_text = (
        f"检测到插件宿主加载器已升级（**Antigravity** `v{cur_ver}`）"
        if has_loader
        else f"检测到 **Antigravity** 已更新至 `v{cur_ver}`"
    )
    write_heal_log(f"Scheduled auto-heal restart for v{cur_ver} ({heal_key[:20]})")
    return make_user_notice(
        f"{reason_text}，插件已预编译就绪，将在约 2 秒后礼貌关闭并自动重启以生效…"
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    explicit_res = None
    explicit_exe = None
    if "--resources-dir" in args:
        idx = args.index("--resources-dir")
        if idx + 1 < len(args):
            explicit_res = args[idx + 1]
            args = args[:idx] + args[idx + 2 :]
    if "--exe" in args:
        idx = args.index("--exe")
        if idx + 1 < len(args):
            explicit_exe = args[idx + 1]
            args = args[:idx] + args[idx + 2 :]

    cmd = args[0] if args else "install"
    if cmd == "status":
        cmd_status(explicit_res)
    elif cmd == "stage":
        cmd_install(stage_only=True, explicit_dir=explicit_res)
    elif cmd == "install":
        cmd_install(stage_only=False, explicit_dir=explicit_res)
    elif cmd == "restore":
        cmd_restore(explicit_res)
    elif cmd == "session-hook":
        print(json.dumps(run_session_hook(), ensure_ascii=False))
    elif cmd == "apply-and-restart":
        res = explicit_res or find_resources_dir()
        if res:
            cmd_apply_and_restart(res, explicit_exe)
    else:
        print(f"未知参数: {cmd} (支持: status, stage, install, restore, session-hook, apply-and-restart)")
