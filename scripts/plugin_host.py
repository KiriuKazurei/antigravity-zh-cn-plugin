"""
Antigravity 插件自包含宿主管理与安全自愈工具
Self-contained host manager for Antigravity UI plugins:
- Cross-minor-version resilient app.asar patching (main.js regex fallback + non-fatal tray.js)
- Monotonic pluginLoader.js version check (upgrade-only, never downgrade across plugins)
- Non-disruptive SessionStart hook: pre-stage + in-app user notice + silent apply on manual exit (never force-kills Antigravity)
- Zero-console-popup Windows background execution (CREATE_NO_WINDOW)
"""

import base64
import csv
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import threading
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
GLOBAL_HEAL_PREF_PATH = os.path.join(PLUGINS_ROOT, ".agy-host-heal.json")
GLOBAL_EXIT_WATCHER_LOCK = os.path.join(PLUGINS_ROOT, ".agy-exit-watcher.lock")

HEAL_COOLDOWN_SEC = 180
NOTICE_COOLDOWN_SEC = 1800
EXIT_WATCHER_MAX_SEC = 24 * 3600
MAX_HEAL_ATTEMPTS_PER_HASH = 2
LOADER_VERSION_RE = re.compile(r'PLUGIN_LOADER_VERSION\s*=\s*["\'](\d+(?:\.\d+)*)["\']')


def _win_no_window_flags():
    if platform.system() == "Windows":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def _win_background_python():
    exe = sys.executable
    if platform.system() == "Windows" and exe:
        base = os.path.basename(exe).lower()
        if base == "python.exe":
            pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(pythonw):
                return pythonw
    return exe


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


def parse_loader_version(raw_bytes):
    if not raw_bytes:
        return (0, 0, 0)
    try:
        head = raw_bytes[:4096].decode("utf-8", errors="ignore")
        m = LOADER_VERSION_RE.search(head)
        if not m:
            return (0, 0, 0)
        parts = [int(x) for x in m.group(1).split(".")]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])
    except Exception:
        return (0, 0, 0)


def format_loader_version(ver_tuple):
    if not ver_tuple or ver_tuple == (0, 0, 0):
        return "0.0.0 (legacy)"
    return ".".join(str(x) for x in ver_tuple)


def get_installed_loader_version(asar_path):
    try:
        raw = read_asar_file(asar_path, ("dist", "pluginLoader.js"))
        if raw is None:
            return None
        return parse_loader_version(raw)
    except Exception:
        return None


def get_local_loader_version():
    try:
        if not os.path.exists(PLUGIN_LOADER_SRC):
            return (0, 0, 0)
        with open(PLUGIN_LOADER_SRC, "rb") as f:
            return parse_loader_version(f.read(4096))
    except Exception:
        return (0, 0, 0)


def needs_loader_upgrade(asar_path):
    """
    Monotonic upgrade check: returns (needs_update: bool, reason: str).
    Never downgrades a newer installed pluginLoader.js to an older copy bundled in another plugin.
    """
    try:
        if not os.path.exists(PLUGIN_LOADER_SRC):
            return (False, "local_missing")
        installed = read_asar_file(asar_path, ("dist", "pluginLoader.js"))
        if installed is None:
            return (True, "missing")
        with open(PLUGIN_LOADER_SRC, "rb") as f:
            expected = f.read()
        installed_ver = parse_loader_version(installed)
        local_ver = parse_loader_version(expected)
        if installed_ver > local_ver:
            return (False, "installed_newer")
        if installed_ver < local_ver:
            return (True, "upgrade")
        if local_ver > (0, 0, 0):
            return (False, "same_version")
        return (installed != expected, "legacy_hash_diff" if installed != expected else "same_legacy")
    except Exception:
        return (False, "error")


def has_current_plugin_loader(asar_path):
    needs_up, _ = needs_loader_upgrade(asar_path)
    return has_plugin_loader(asar_path) and not needs_up


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

    tmp_dst = f"{dst_asar}.{os.getpid()}.tmp"
    try:
        write_asar(tmp_dst, header, file_bytes)

        # Verify the generated archive can be parsed and contains the loader
        _, verify_bytes = load_asar(tmp_dst)
        if ("dist", "pluginLoader.js") not in verify_bytes:
            raise RuntimeError("校验失败: 生成的 asar 中缺少 dist/pluginLoader.js")
        if 'require("./pluginLoader")' not in verify_bytes[("dist", "main.js")].decode("utf-8"):
            raise RuntimeError("校验失败: 生成的 asar 中 dist/main.js 未包含 pluginLoader 引用")

        os.replace(tmp_dst, dst_asar)
    finally:
        if os.path.exists(tmp_dst):
            try:
                os.remove(tmp_dst)
            except Exception:
                pass


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
    preserve = {
        "loader.log",
        "heal.log",
        "heal_state.json",
        "kb-config.json",
        "notify.log",
        "notifier-config.json",
        "snippets.json",
        "usage-history.json",
    }
    for root, dirs, files in os.walk(PLUGIN_DIR):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        rel_root = os.path.relpath(root, PLUGIN_DIR)
        dst_root = dst_dir if rel_root == "." else os.path.join(dst_dir, rel_root)
        os.makedirs(dst_root, exist_ok=True)
        for fn in files:
            if fn.endswith((".pyc", ".tmp")):
                continue
            if rel_root == "runtime" and fn in preserve and os.path.exists(os.path.join(dst_root, fn)):
                continue
            shutil.copy2(os.path.join(root, fn), os.path.join(dst_root, fn))
    print(f"已同步插件文件至: {dst_dir}")
    return dst_dir


def _atomic_write_json(path, data):
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def load_global_heal_pref():
    return read_json_safe(GLOBAL_HEAL_PREF_PATH, is_jsonc=False) or {}


def save_global_heal_pref(pref):
    _atomic_write_json(GLOBAL_HEAL_PREF_PATH, pref)


def _get_heal_disable_state():
    if os.environ.get("AGY_DISABLE_AUTO_HEAL", "").strip() in ("1", "true", "TRUE", "yes"):
        return (True, "已禁用 (环境变量 AGY_DISABLE_AUTO_HEAL=1)")
    pref = load_global_heal_pref()
    if pref.get("disabled_permanent"):
        return (True, "已永久禁用 (运行 enable-heal 恢复)")
    disabled_until = pref.get("disabled_until", 0)
    if isinstance(disabled_until, (int, float)) and disabled_until > time.time():
        remain_h = max(0.1, (disabled_until - time.time()) / 3600.0)
        return (True, f"已暂时禁用 (剩余约 {remain_h:.1f} 小时)")
    return (False, "已启用 (仅程序内提醒 + 手动退出后静默替换，绝不强杀进程)")


def is_auto_heal_disabled():
    return _get_heal_disable_state()[0]


def get_heal_disable_summary():
    return _get_heal_disable_state()[1]


def cmd_disable_heal(hours=24, permanent=False, explicit_dir=None):
    pref = load_global_heal_pref()
    if permanent:
        pref["disabled_permanent"] = True
        pref["disabled_until"] = 0
    else:
        pref["disabled_permanent"] = False
        pref["disabled_until"] = int(time.time() + max(1, hours) * 3600)
    save_global_heal_pref(pref)

    # Cancel any pending staged package so exit watcher exits cleanly
    res_dir, _, _, staged_path, _ = get_paths(explicit_dir)
    if res_dir and staged_path and os.path.exists(staged_path):
        try:
            os.remove(staged_path)
            print(f"已取消待应用补丁包: {staged_path}")
        except Exception:
            pass

    if permanent:
        print("[成功] 已永久暂停插件宿主自愈提醒（随时可运行 enable-heal 恢复）。")
    else:
        print(f"[成功] 已暂时禁用插件宿主自愈提醒 {max(1, hours)} 小时（随时可运行 enable-heal 恢复）。")


def cmd_enable_heal():
    pref = load_global_heal_pref()
    pref["disabled_permanent"] = False
    pref["disabled_until"] = 0
    pref["last_notice_ts"] = 0
    save_global_heal_pref(pref)
    print("[成功] 已恢复插件宿主自愈提醒（模式：程序内提醒 + 手动退出后静默更新）。")


def cmd_status(explicit_dir=None):
    res_dir, asar_path, bak_path, staged_path, _ = get_paths(explicit_dir)
    print(f"=== Antigravity 插件宿主状态检查 ({PLUGIN_NAME}) ===")
    print(f"- 插件目录: {PLUGIN_DIR}")
    print(f"- 插件启用: {is_plugin_enabled()}")
    print(f"- 自愈策略: {get_heal_disable_summary()}")
    local_ver = get_local_loader_version()
    print(f"- 本地 pluginLoader 版本: {format_loader_version(local_ver)}")
    print(f"- 探测到安装目录: {res_dir or '未找到'}")
    if res_dir:
        installed_ver = get_installed_loader_version(asar_path)
        needs_up, up_reason = needs_loader_upgrade(asar_path)
        print(f"- app.asar 存在: {os.path.exists(asar_path)} (版本: {get_asar_version(asar_path)})")
        print(
            f"- app.asar.bak 备份存在: {os.path.exists(bak_path)}"
            + (f" (版本: {get_asar_version(bak_path)})" if os.path.exists(bak_path) else "")
        )
        print(f"- app.asar 已安装宿主加载器: {has_plugin_loader(asar_path)} (版本: {format_loader_version(installed_ver) if installed_ver is not None else '未安装'})")
        print(f"- 需要升级宿主加载器: {needs_up} ({up_reason})")
        print(f"- app.asar 存在旧版失效注入: {has_legacy_preload_patch(asar_path)}")
        print(f"- app.asar.staged 待应用包存在: {os.path.exists(staged_path)}")
    print(f"- 宿主注入日志存在: {os.path.exists(os.path.join(RUNTIME_DIR, 'loader.log'))}")
    print(f"- 自愈日志存在: {os.path.exists(HEAL_LOG_PATH)}")


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
        os.replace(staged_path, asar_path)
        print("[成功] 宿主加载器已直接安装至 app.asar，启动 Antigravity 即可生效！")
    except PermissionError:
        print("[就绪] app.asar 当前正被运行中的 Antigravity 占用，已生成 app.asar.staged 等待退出后替换。")


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
    _atomic_write_json(HEAL_STATE_PATH, state)


def _is_pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        if platform.system() == "Windows":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_ACCESS_DENIED = 5
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return ctypes.get_last_error() == ERROR_ACCESS_DENIED
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _is_exit_watcher_running():
    lock_data = read_json_safe(GLOBAL_EXIT_WATCHER_LOCK, is_jsonc=False)
    if not isinstance(lock_data, dict):
        return False
    pid = lock_data.get("pid")
    started = lock_data.get("started_at", 0)
    if not isinstance(started, (int, float)) or time.time() - started > EXIT_WATCHER_MAX_SEC + 60:
        return False
    return _is_pid_alive(pid)


def _acquire_exit_watcher_lock(resources_dir):
    """Atomically acquire single-instance lock using O_CREAT | O_EXCL."""
    os.makedirs(PLUGINS_ROOT, exist_ok=True)
    payload = json.dumps(
        {"pid": os.getpid(), "started_at": int(time.time()), "resources_dir": resources_dir}
    ).encode("utf-8")
    for _ in range(2):
        try:
            fd = os.open(GLOBAL_EXIT_WATCHER_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            if _is_exit_watcher_running():
                return False
            # Stale lock left by dead/timed-out process: remove and retry once
            try:
                os.remove(GLOBAL_EXIT_WATCHER_LOCK)
            except OSError:
                return False
        except OSError:
            return False
    return False


def _win_spawn_via_wmi(cmd_args):
    """
    Spawn a completely detached background process via Windows WMI
    (Win32_Process.Create). Because WmiPrvSE.exe creates the process, it escapes
    any restrictive parent Windows Job Object (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    without BREAKAWAY_OK) so it survives Antigravity / hook exit.
    """
    try:
        cmd_line = subprocess.list2cmdline([str(a) for a in cmd_args])
        ps_cmd_literal = "'" + cmd_line.replace("'", "''") + "'"
        exe_base = os.path.basename(str(cmd_args[0])).lower() if cmd_args else ""
        # pythonw.exe has no console window; avoid SW_HIDE so its tkinter popup is always visible
        show_val = 5 if exe_base == "pythonw.exe" else 0
        ps_script = (
            "$si = New-CimInstance -ClassName Win32_ProcessStartup "
            f"-Property @{{ShowWindow=[uint16]{show_val}}} -ClientOnly; "
            f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            f"-Arguments @{{CommandLine={ps_cmd_literal}; ProcessStartupInformation=$si}}; "
            'Write-Output ("RC=" + $r.ReturnValue + " PID=" + $r.ProcessId)'
        )
        encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            creationflags=_win_no_window_flags(),
        )
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        return proc.returncode == 0 and "RC=0" in out
    except Exception:
        return False


def _spawn_background_script(cmd_args):
    if platform.system() == "Windows":
        if _win_spawn_via_wmi(cmd_args):
            write_heal_log("Spawned background watcher via WMI (Win32_Process.Create).")
            return
        CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(
                cmd_args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=flags | CREATE_BREAKAWAY_FROM_JOB,
            )
            write_heal_log("Spawned background watcher via CREATE_BREAKAWAY_FROM_JOB.")
            return
        except OSError:
            pass
        bg_python = _win_background_python()
        # Fallback: double-fork breaks parent-child PID link without opening any console window
        launcher_code = (
            "import subprocess; "
            f"subprocess.Popen({cmd_args!r}, "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
            f"close_fds=True, creationflags={flags})"
        )
        mid = subprocess.Popen(
            [bg_python, "-c", launcher_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
        mid.wait(timeout=3)
        write_heal_log("Spawned background watcher via double-fork fallback.")
    else:
        subprocess.Popen(
            cmd_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )


def spawn_exit_watcher(resources_dir):
    if _is_exit_watcher_running():
        return
    bg_python = _win_background_python()
    cmd_args = [
        bg_python,
        os.path.abspath(__file__),
        "apply-on-exit",
        "--resources-dir",
        resources_dir,
    ]
    _spawn_background_script(cmd_args)


def spawn_detached_healer(resources_dir, exe_path):
    """Explicit manual restart helper (never called automatically by SessionStart hook)."""
    bg_python = _win_background_python()
    cmd_args = [
        bg_python,
        os.path.abspath(__file__),
        "apply-and-restart",
        "--resources-dir",
        resources_dir,
    ]
    if exe_path:
        cmd_args.extend(["--exe", exe_path])
    _spawn_background_script(cmd_args)


def _win_is_process_running_fast(exe_name):
    """Fast (~15ms), zero-subprocess Windows process check via CreateToolhelp32Snapshot."""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        raise OSError(f"CreateToolhelp32Snapshot failed: {ctypes.get_last_error()}")
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        target = exe_name.lower()
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.lower() == target:
                    return True
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
        return False
    finally:
        kernel32.CloseHandle(snap)


def is_antigravity_running(default_on_error=True):
    system = platform.system()
    try:
        if system == "Windows":
            return _win_is_process_running_fast("Antigravity.exe")
        if system == "Darwin":
            rc = subprocess.run(["pgrep", "-x", "Antigravity"], capture_output=True).returncode
            return rc == 0
        rc = subprocess.run(["pgrep", "-f", "antigravity"], capture_output=True).returncode
        return rc == 0
    except Exception:
        return default_on_error


def polite_close_antigravity():
    """Only used when the user explicitly runs manual `apply-and-restart`."""
    system = platform.system()
    if system == "Windows":
        no_win = _win_no_window_flags()
        write_heal_log("Sending polite close (WM_CLOSE) to Antigravity.exe...")
        subprocess.run(
            ["taskkill", "/IM", "Antigravity.exe"],
            capture_output=True,
            timeout=5,
            creationflags=no_win,
        )
        for _ in range(12):  # Wait up to 6s
            time.sleep(0.5)
            if not is_antigravity_running():
                write_heal_log("Antigravity.exe exited gracefully.")
                return
        write_heal_log("Antigravity.exe still running (likely tray background mode); closing tree...")
        subprocess.run(
            ["taskkill", "/IM", "Antigravity.exe", "/F", "/T"],
            capture_output=True,
            timeout=5,
            creationflags=no_win,
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
        CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            [exe_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
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


def _swap_staged_asar(asar_path, staged_path, retries=25):
    last_err = None
    for _ in range(retries):
        try:
            if not os.path.exists(staged_path):
                return (False, "app.asar.staged missing")
            os.replace(staged_path, asar_path)
            return (True, None)
        except PermissionError as exc:
            last_err = exc
            time.sleep(0.3)
        except Exception as exc:
            last_err = exc
            break
    return (False, last_err)


def _show_fallback_result_popup(success, title, message):
    """Fallback native OS dialog when tkinter is unavailable."""
    system = platform.system()
    try:
        if system == "Windows":
            import ctypes
            # MB_OK (0x0) | MB_TOPMOST (0x40000) | MB_SETFOREGROUND (0x10000) | ICON
            flags = 0x50000 | (0x40 if success else 0x10)
            ctypes.windll.user32.MessageBoxW(0, str(message), str(title), flags)
        elif system == "Darwin":
            safe_msg = str(message).replace("\\", "\\\\").replace('"', '\\"')
            safe_title = str(title).replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e", f'display dialog "{safe_msg}" with title "{safe_title}" buttons {{"确定"}} default button "确定"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        else:
            icon = "info" if success else "error"
            if shutil.which("zenity"):
                subprocess.run(
                    ["zenity", f"--{icon}", f"--title={title}", f"--text={message}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                )
            elif shutil.which("notify-send"):
                subprocess.run(
                    ["notify-send", str(title), str(message)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
    except Exception:
        pass


def _run_swap_with_popup(resources_dir, asar_path, staged_path, app_ver, loader_ver_str, auto_relaunch=False, exe_path=None):
    """
    Display a clean topmost progress & result popup while applying app.asar.staged -> app.asar.
    Returns (replaced: bool, err: Any, relaunched: bool).
    """
    title_text = "Antigravity 汉化插件自愈"
    ver_label = f"v{app_ver}" if app_ver else "当前版本"
    target_exe = exe_path or find_antigravity_exe(resources_dir)
    result = {"replaced": False, "err": None, "relaunched": False}

    try:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title(title_text)
        root.resizable(False, False)
        root.attributes("-topmost", True)

        width, height = 430, 195
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        pos_x = max(0, (screen_w - width) // 2)
        pos_y = max(0, (screen_h - height) // 2)
        root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
        root.deiconify()
        root.lift()
        try:
            root.focus_force()
        except Exception:
            pass

        container = ttk.Frame(root, padding=(20, 16, 20, 16))
        container.pack(fill="both", expand=True)

        status_var = tk.StringVar(value="正在安装汉化插件宿主...")
        detail_var = tk.StringVar(
            value=f"正在为 Antigravity {ver_label} 覆盖更新 app.asar（宿主 {loader_ver_str}），\n请稍候（通常仅需 1~2 秒），期间请勿立即重新打开软件。"
        )

        status_lbl = tk.Label(
            container,
            textvariable=status_var,
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
            justify="left",
        )
        status_lbl.pack(fill="x", pady=(0, 8))

        detail_lbl = tk.Label(
            container,
            textvariable=detail_var,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
            justify="left",
            wraplength=385,
        )
        detail_lbl.pack(fill="x", pady=(0, 12))

        pbar = ttk.Progressbar(container, mode="indeterminate", length=385)
        pbar.pack(fill="x", pady=(0, 12))
        pbar.start(12)

        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill="x", side="bottom")

        def close_window():
            try:
                root.destroy()
            except Exception:
                pass

        def launch_and_close():
            if target_exe and os.path.exists(target_exe):
                relaunch_antigravity(target_exe)
                result["relaunched"] = True
            close_window()

        def clear_buttons():
            for child in btn_frame.winfo_children():
                child.destroy()

        def start_swap_worker():
            clear_buttons()
            status_lbl.config(fg="#1f2937")
            status_var.set("正在安装汉化插件宿主...")
            detail_var.set(
                f"正在为 Antigravity {ver_label} 覆盖更新 app.asar（宿主 {loader_ver_str}），\n请稍候（通常仅需 1~2 秒），期间请勿立即重新打开软件。"
            )
            if not pbar.winfo_ismapped():
                pbar.pack(fill="x", pady=(0, 12), before=btn_frame)
            pbar.start(12)

            def _worker():
                replaced, err = _swap_staged_asar(asar_path, staged_path, retries=25)
                result["replaced"] = replaced
                result["err"] = err

                def _on_done():
                    try:
                        pbar.stop()
                        pbar.pack_forget()
                    except Exception:
                        pass
                    clear_buttons()
                    if replaced:
                        status_lbl.config(fg="#15803d")
                        status_var.set("✓ 汉化插件宿主安装成功！")
                        if auto_relaunch and target_exe and os.path.exists(target_exe):
                            detail_var.set(
                                f"已成功更新 Antigravity {ver_label}（宿主 {loader_ver_str}），正在自动重新启动 Antigravity..."
                            )
                            ttk.Button(btn_frame, text="立即启动", command=launch_and_close).pack(side="right", padx=(6, 0))
                            ttk.Button(btn_frame, text="关闭", command=close_window).pack(side="right")
                            root.after(1500, launch_and_close)
                        else:
                            detail_var.set(
                                f"已成功更新 Antigravity {ver_label}（宿主 {loader_ver_str}）。\n点击下方按钮或手动重新打开 Antigravity 即可恢复简体中文界面。"
                            )
                            ttk.Button(btn_frame, text="关闭", command=close_window).pack(side="right", padx=(6, 0))
                            if target_exe and os.path.exists(target_exe):
                                ttk.Button(btn_frame, text="立即启动 Antigravity", command=launch_and_close).pack(side="right")
                    else:
                        status_lbl.config(fg="#b91c1c")
                        status_var.set("✕ 汉化插件宿主安装失败")
                        detail_var.set(
                            f"覆盖 app.asar 失败：{err}\n请确认 Antigravity 已完全退出（包括系统托盘），再点击「重试」。"
                        )
                        ttk.Button(btn_frame, text="关闭", command=close_window).pack(side="right", padx=(6, 0))
                        ttk.Button(btn_frame, text="重试", command=start_swap_worker).pack(side="right")

                try:
                    root.after(0, _on_done)
                except Exception:
                    pass

            threading.Thread(target=_worker, daemon=True).start()

        root.after(50, start_swap_worker)
        root.mainloop()
        return (result["replaced"], result["err"], result["relaunched"])
    except Exception as ui_exc:
        write_heal_log(f"Tkinter popup unavailable ({ui_exc}); falling back to native dialog.")
        replaced, err = _swap_staged_asar(asar_path, staged_path, retries=25)
        if replaced:
            _show_fallback_result_popup(
                True,
                title_text,
                f"已成功为 Antigravity {ver_label} 安装汉化插件宿主（{loader_ver_str}）。\n现在可以重新打开 Antigravity 恢复简体中文界面。",
            )
            if auto_relaunch and target_exe and os.path.exists(target_exe):
                relaunch_antigravity(target_exe)
                return (True, None, True)
            return (True, None, False)
        _show_fallback_result_popup(
            False,
            title_text,
            f"覆盖 app.asar 失败：{err}\n请确认 Antigravity 已完全退出（包括系统托盘）后重试。",
        )
        return (False, err, False)


def cmd_apply_on_exit(resources_dir):
    """
    Non-disruptive exit watcher: waits silently until the user manually closes Antigravity,
    then displays a progress/result popup while replacing app.asar with app.asar.staged.
    Never kills Antigravity.
    """
    if not _acquire_exit_watcher_lock(resources_dir):
        return

    asar_path = os.path.join(resources_dir, "app.asar")
    staged_path = os.path.join(resources_dir, "app.asar.staged")
    write_heal_log(f"Exit watcher started (pid={os.getpid()}, resources_dir={resources_dir}). Waiting for manual exit...")

    deadline = time.time() + EXIT_WATCHER_MAX_SEC
    try:
        while time.time() < deadline:
            if is_auto_heal_disabled():
                write_heal_log("Exit watcher stopped: auto-heal disabled by user.")
                return
            if not os.path.exists(staged_path):
                write_heal_log("Exit watcher stopped: app.asar.staged no longer present.")
                return
            if not is_antigravity_running(default_on_error=True):
                time.sleep(0.3)
                if is_antigravity_running(default_on_error=True):
                    continue
                if is_auto_heal_disabled() or not os.path.exists(staged_path):
                    return
                # Guard against downgrading Antigravity itself if official auto-update upgraded app.asar
                staged_app_ver = get_asar_version(staged_path)
                active_app_ver = get_asar_version(asar_path)
                if staged_app_ver and active_app_ver and staged_app_ver != active_app_ver:
                    write_heal_log(
                        f"Exit watcher aborted: app.asar version changed ({staged_app_ver} -> {active_app_ver}); removing stale staged file."
                    )
                    try:
                        os.remove(staged_path)
                    except Exception:
                        pass
                    return
                # Guard against downgrading pluginLoader if app.asar was already updated
                staged_ver = get_installed_loader_version(staged_path) or (0, 0, 0)
                active_ver = get_installed_loader_version(asar_path)
                if active_ver is not None and active_ver >= staged_ver and staged_ver > (0, 0, 0):
                    write_heal_log(
                        f"Exit watcher skipped swap: active loader ({format_loader_version(active_ver)}) >= staged ({format_loader_version(staged_ver)})."
                    )
                    try:
                        os.remove(staged_path)
                    except Exception:
                        pass
                    return
                loader_ver_str = format_loader_version(staged_ver)
                replaced, err, _ = _run_swap_with_popup(
                    resources_dir,
                    asar_path,
                    staged_path,
                    staged_app_ver or active_app_ver,
                    loader_ver_str,
                    auto_relaunch=False,
                )
                if replaced:
                    ver = get_asar_version(asar_path)
                    write_heal_log(f"Successfully applied app.asar on manual exit (v{ver}, loader {loader_ver_str}).")
                    state = load_heal_state()
                    state["last_success_ts"] = int(time.time())
                    state["last_success_version"] = ver
                    save_heal_state(state)
                    return
                if is_antigravity_running(default_on_error=True):
                    write_heal_log(f"Swap deferred (Antigravity restarted quickly): {err}")
                    continue
                write_heal_log(f"Failed to replace app.asar on manual exit: {err}")
                return
            time.sleep(0.5)
        write_heal_log("Exit watcher timed out after 24h without Antigravity exit.")
    except Exception as exc:
        write_heal_log(f"Exit watcher unexpected error: {exc}")
    finally:
        try:
            lock_data = read_json_safe(GLOBAL_EXIT_WATCHER_LOCK, is_jsonc=False)
            if isinstance(lock_data, dict) and lock_data.get("pid") == os.getpid():
                os.remove(GLOBAL_EXIT_WATCHER_LOCK)
        except Exception:
            pass


def cmd_apply_and_restart(resources_dir, exe_path=None):
    """Explicit manual restart command (never triggered automatically during work)."""
    write_heal_log(f"Manual apply-and-restart started (resources_dir={resources_dir}, exe={exe_path})")
    asar_path = os.path.join(resources_dir, "app.asar")
    staged_path = os.path.join(resources_dir, "app.asar.staged")
    if not os.path.exists(staged_path):
        write_heal_log("Aborted: app.asar.staged missing.")
        return

    time.sleep(1.0)
    polite_close_antigravity()

    staged_app_ver = get_asar_version(staged_path) or get_asar_version(asar_path)
    staged_ver = get_installed_loader_version(staged_path) or (0, 0, 0)
    loader_ver_str = format_loader_version(staged_ver)
    target_exe = exe_path or find_antigravity_exe(resources_dir)

    replaced, last_err, relaunched = _run_swap_with_popup(
        resources_dir,
        asar_path,
        staged_path,
        staged_app_ver,
        loader_ver_str,
        auto_relaunch=True,
        exe_path=target_exe,
    )
    if not replaced:
        write_heal_log(f"Failed to replace app.asar: {last_err}")
        return

    ver = get_asar_version(asar_path)
    write_heal_log(f"Successfully patched app.asar (v{ver}).")
    state = load_heal_state()
    state["last_success_ts"] = int(time.time())
    state["last_success_version"] = ver
    save_heal_state(state)

    if not relaunched:
        relaunch_antigravity(target_exe)


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

    if not is_plugin_enabled() or is_auto_heal_disabled():
        return {}

    res_dir, asar_path, bak_path, staged_path, _ = get_paths()
    if not res_dir or not os.path.exists(asar_path):
        return {}

    # Fast path: monotonic version check (never downgrades a newer installed loader)
    needs_up, up_reason = needs_loader_upgrade(asar_path)
    if not needs_up:
        return {}

    cur_ver = get_asar_version(asar_path) or "unknown"
    script_path = os.path.join(SCRIPT_DIR, "plugin_host.py")
    if has_legacy_preload_patch(asar_path):
        return make_user_notice(
            f"检测到旧版失效注入残留，请先手动退出 **Antigravity** 后运行：`python \"{script_path}\" restore` 再执行 `install`。"
        )

    local_ver = get_local_loader_version()
    installed_ver = get_installed_loader_version(asar_path)
    asar_hash = file_sha256(asar_path)
    loader_hash = file_sha256(PLUGIN_LOADER_SRC) if os.path.exists(PLUGIN_LOADER_SRC) else ""
    heal_key = f"{asar_hash}:{loader_hash}"
    now = int(time.time())

    # Pre-stage only if not already staged for the same app version with >= local loader version
    need_stage = True
    if os.path.exists(staged_path):
        staged_app_ver = get_asar_version(staged_path)
        staged_ver = get_installed_loader_version(staged_path) or (0, 0, 0)
        same_app_ver = bool(staged_app_ver and cur_ver != "unknown" and staged_app_ver == cur_ver)
        if same_app_ver and staged_ver >= local_ver and local_ver > (0, 0, 0):
            need_stage = False
        elif same_app_ver and time.time() - os.path.getmtime(staged_path) < 45 and staged_ver >= local_ver:
            need_stage = False

    if need_stage:
        try:
            ensure_clean_backup(asar_path, bak_path, quiet=True)
            build_patched_asar(bak_path, staged_path, quiet=True)
            write_heal_log(
                f"Pre-staged app.asar.staged for v{cur_ver} (loader {format_loader_version(installed_ver) if installed_ver is not None else 'none'} -> {format_loader_version(local_ver)}, reason={up_reason})"
            )
        except Exception as exc:
            write_heal_log(f"Pre-stage failed for v{cur_ver}: {exc}")
            return make_user_notice(
                f"检测到 **Antigravity**（`v{cur_ver}`）需要更新插件宿主，但预构建失败（未做任何中断操作，不影响正常使用）：`{exc}`"
            )

    # Start silent, non-disruptive exit watcher (never kills Antigravity)
    try:
        spawn_exit_watcher(res_dir)
    except Exception as exc:
        write_heal_log(f"Failed to spawn exit watcher: {exc}")

    # Global notice cooldown with atomic short lock so concurrent hooks never spam duplicate notices
    notice_lock = os.path.join(PLUGINS_ROOT, ".agy-notice.lock")
    claimed_lock = False
    try:
        os.makedirs(PLUGINS_ROOT, exist_ok=True)
        try:
            fd = os.open(notice_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            claimed_lock = True
        except FileExistsError:
            # Another concurrent hook is currently emitting a notice; clear only if stale (>15s)
            try:
                if time.time() - os.path.getmtime(notice_lock) > 15:
                    os.remove(notice_lock)
            except OSError:
                pass
            return {}

        pref = load_global_heal_pref()
        last_notice_key = pref.get("last_notice_key", "")
        last_notice_ts = pref.get("last_notice_ts", 0)
        if last_notice_key == heal_key and (now - last_notice_ts) < NOTICE_COOLDOWN_SEC:
            return {}

        pref["last_notice_key"] = heal_key
        pref["last_notice_ts"] = now
        save_global_heal_pref(pref)
    finally:
        if claimed_lock:
            try:
                os.remove(notice_lock)
            except OSError:
                pass

    reason_text = (
        f"检测到插件宿主加载器有新版本（`{format_loader_version(installed_ver)}` → `{format_loader_version(local_ver)}`）"
        if installed_ver is not None
        else f"检测到 **Antigravity**（`v{cur_ver}`）尚未注入插件宿主加载器（`{format_loader_version(local_ver)}`）"
    )
    return make_user_notice(
        f"**[插件宿主更新提醒]** {reason_text}。\n\n"
        f"补丁包 `app.asar.staged` 已安全预编译就绪，**不会强制关闭或打断你当前的工作**。请选择：\n"
        f"- **手动重启生效**：在你方便时手动完全退出 **Antigravity**，后台监听会在软件自然退出后静默完成替换，下次打开即生效；\n"
        f"- **暂时禁用提醒**：若当前不想更新，可运行 `python \"{script_path}\" disable-heal`（默认暂停 24 小时，加 `--permanent` 永久暂停，随时可用 `enable-heal` 恢复）。"
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    explicit_res = None
    explicit_exe = None
    disable_hours = 24
    disable_permanent = False
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
    if "--hours" in args:
        idx = args.index("--hours")
        if idx + 1 < len(args):
            try:
                disable_hours = int(args[idx + 1])
            except ValueError:
                disable_hours = 24
            args = args[:idx] + args[idx + 2 :]
    if "--permanent" in args:
        disable_permanent = True
        args = [a for a in args if a != "--permanent"]

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
    elif cmd == "apply-on-exit":
        res = explicit_res or find_resources_dir()
        if res:
            cmd_apply_on_exit(res)
    elif cmd == "apply-and-restart":
        res = explicit_res or find_resources_dir()
        if res:
            cmd_apply_and_restart(res, explicit_exe)
    elif cmd == "disable-heal":
        cmd_disable_heal(hours=disable_hours, permanent=disable_permanent, explicit_dir=explicit_res)
    elif cmd == "enable-heal":
        cmd_enable_heal()
    else:
        print(
            f"未知参数: {cmd} (支持: status, stage, install, restore, session-hook, apply-on-exit, apply-and-restart, disable-heal, enable-heal)"
        )
