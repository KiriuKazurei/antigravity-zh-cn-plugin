"""
SessionStart hook wrapper for antigravity-zh-cn.
Always outputs valid JSON and exits 0 so it never blocks a conversation turn.
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    import plugin_host

    result = plugin_host.run_session_hook()
    print(json.dumps(result if isinstance(result, dict) else {}, ensure_ascii=False))
except Exception as exc:
    try:
        log_path = os.path.join(
            os.path.dirname(SCRIPT_DIR), "runtime", "heal.log"
        )
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[session_hook error] {exc}\n")
    except Exception:
        pass
    print("{}")
