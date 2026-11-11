"""Hermes CoBrowse Plugin Entrypoint.

Registers the CoBrowse co-browsing toolset and telemetry hooks with Hermes Agent.
Can also be executed standalone as a local test server.
"""

import logging
import sys
from hermes_cobrowse.schemas import SCHEMAS
from hermes_cobrowse.tools import (
    CoBrowseManager,
    handle_click,
    handle_close,
    handle_eval_js,
    handle_get_state,
    handle_navigate,
    handle_open,
    handle_request_handoff,
    handle_scroll,
    handle_type,
)

logger = logging.getLogger("cobrowse")

TOOL_MAP = {
    "cobrowse_open": handle_open,
    "cobrowse_navigate": handle_navigate,
    "cobrowse_get_state": handle_get_state,
    "cobrowse_click": handle_click,
    "cobrowse_type": handle_type,
    "cobrowse_request_handoff": handle_request_handoff,
    "cobrowse_scroll": handle_scroll,
    "cobrowse_eval_js": handle_eval_js,
    "cobrowse_close": handle_close,
}


def on_post_tool_call(tool_name: str, args: dict = None, result: str = None, **kwargs):
    """Hermes Agent post-tool hook: broadcasts live tool execution telemetry."""
    if tool_name.startswith("cobrowse_"):
        mgr = CoBrowseManager.get_instance()
        sid = args.get("session_id") if isinstance(args, dict) else None
        eng = mgr.get_session(sid) if sid else mgr.engine
        if eng and mgr.loop and mgr.loop.is_running():
            telemetry_event = {
                "type": "agent_action",
                "session_id": eng.session_id,
                "action": "tool_call",
                "tool": tool_name,
                "label": f"Hermes completed tool: {tool_name}",
            }
            try:
                mgr.run_async(eng.broadcast_event(telemetry_event), timeout=2.0)
            except Exception:
                pass


def register(ctx):
    """Hermes Plugin registration callback."""
    logger.info("Registering Hermes CoBrowse plugin...")

    # 1. Register Tools
    for tool_name, handler_fn in TOOL_MAP.items():
        schema = SCHEMAS.get(tool_name)
        if schema:
            ctx.register_tool(
                name=tool_name,
                toolset="cobrowse",
                schema=schema,
                handler=handler_fn,
            )

    # 2. Register Lifecycle Hook
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("post_tool_call", on_post_tool_call)

    logger.info("Hermes CoBrowse plugin successfully registered!")


if __name__ == "__main__":
    import time
    import webbrowser

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 65)
    print("  HERMES COBROWSE — DUAL-CONTROL CO-BROWSING RUNTIME")
    print("=" * 65)

    target_url = sys.argv[1] if len(sys.argv) > 1 else "https://news.ycombinator.com"
    print(f"[*] Booting CoBrowse browser engine on: {target_url}")

    mgr = CoBrowseManager.get_instance()
    res = mgr.ensure_started(initial_url=target_url)

    preview_url = mgr.preview_url
    print(f"[+] CoBrowse Server live at: {preview_url}")
    print("[+] Opening browser canvas...")
    webbrowser.open(preview_url)

    print("\nPress Ctrl+C to terminate.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down CoBrowse...")
        mgr.shutdown()
        print("Shutdown complete.")
