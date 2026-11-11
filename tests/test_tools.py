"""Unit & Integration tests for Hermes Tool Handlers, Server, Portal, and Registration."""

import importlib.util
import json
import os
import sys
import urllib.request
import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient
from demo_portal.app import portal_app
from hermes_cobrowse import register
from hermes_cobrowse.tools import (
    handle_click,
    handle_close,
    handle_eval_js,
    handle_get_state,
    handle_navigate,
    handle_open,
    handle_scroll,
)


import websocket

def _verify_websocket_stream(preview_url: str):
    ws_url = preview_url.replace("http://", "ws://") + "/ws/stream"
    ws = websocket.create_connection(ws_url, timeout=5)
    try:
        init_msg = json.loads(ws.recv())
        assert init_msg["type"] == "init"
        assert init_msg["running"] is True
        frame_msg = json.loads(ws.recv())
        assert frame_msg["type"] == "frame" and len(frame_msg["data"]) > 100
        ws.send(json.dumps({"type": "takeover_toggle", "enabled": True}))
        takeover_msg = json.loads(ws.recv())
        assert takeover_msg["type"] == "takeover_status" and takeover_msg["enabled"] is True
    finally:
        ws.close()
    print("  [✓] Real WebSocket /ws/stream init, JPEG frame, and takeover_toggle verified.")


class MockContext:
    def __init__(self):
        self.registered_tools = {}
        self.registered_hooks = {}

    def register_tool(self, name, toolset, schema, handler):
        self.registered_tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
        }

    def register_hook(self, hook_name, callback):
        self.registered_hooks[hook_name] = callback


def test_registration_and_manifest():
    print("[*] Testing Hermes plugin manifest & root entrypoint...")
    with open(os.path.join(PROJECT_ROOT, "plugin.yaml"), "r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)

    assert manifest["name"] == "hermes-cobrowse"
    assert "provides_tools" in manifest and len(manifest["provides_tools"]) == 9
    assert "post_tool_call" in manifest["provides_hooks"]

    # Load root __init__.py the same way Hermes plugin loader does
    root_init = os.path.join(PROJECT_ROOT, "__init__.py")
    spec = importlib.util.spec_from_file_location("hermes_cobrowse_root", root_init)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ctx = MockContext()
    mod.register(ctx)

    assert len(ctx.registered_tools) == 9, f"Expected 9 tools, got {len(ctx.registered_tools)}"
    for tool_name in manifest["provides_tools"]:
        assert tool_name in ctx.registered_tools, f"Missing tool {tool_name}"
    assert "post_tool_call" in ctx.registered_hooks
    print("  [✓] Manifest, root __init__.py, 9 tools, and post_tool_call hook verified.")


def test_demo_portal_routes():
    print("[*] Testing Demo Portal form routes (zero python-multipart)...")
    client = TestClient(portal_app)

    r_login_page = client.get("/login")
    assert r_login_page.status_code == 200
    assert "Apex Cloud Console" in r_login_page.text

    r_login_post = client.post(
        "/login",
        data={"email": "admin@nousresearch.com", "password": "secret"},
        follow_redirects=False,
    )
    assert r_login_post.status_code == 303
    assert r_login_post.headers["location"] == "/2fa"

    r_bad_otp = client.post("/verify-2fa", data={"code": "111111"}, follow_redirects=False)
    assert r_bad_otp.status_code == 400

    r_good_otp = client.post("/verify-2fa", data={"code": "849201"}, follow_redirects=False)
    assert r_good_otp.status_code == 303
    assert r_good_otp.headers["location"] == "/dashboard"

    r_dash = client.get("/dashboard")
    assert r_dash.status_code == 200
    assert "Deploy Hermes Cluster" in r_dash.text
    print("  [✓] Demo portal login, 2FA validation, and dashboard verified.")


def test_tool_invocations_and_server():
    print("[*] Testing Tool Handlers, Server HTTP/WS endpoints, and lifecycle...")

    # 1. cobrowse_open
    open_res = json.loads(handle_open({"url": "about:blank", "viewport_width": 1024, "viewport_height": 768}))
    assert open_res.get("status") in ["started", "already_running"]
    preview_url = open_res.get("preview_url")
    assert preview_url and preview_url.startswith("http://127.0.0.1:")
    print(f"  [✓] cobrowse_open succeeded immediately: {preview_url}")

    # 2. Verify HTTP endpoints are immediately listening (no startup race!)
    with urllib.request.urlopen(f"{preview_url}/health", timeout=3) as resp:
        health = json.loads(resp.read().decode("utf-8"))
        assert health["status"] == "healthy"
        assert health["running"] is True

    # Verify empty POST /api/resume works without 422
    req = urllib.request.Request(f"{preview_url}/api/resume", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=3) as resp:
        resume_json = json.loads(resp.read().decode("utf-8"))
        assert resume_json["status"] == "ok"
    print("  [✓] Streaming server /health and empty-body /api/resume verified.")

    _verify_websocket_stream(preview_url)

    # 3. Second cobrowse_open still preserves preview_url
    open_res2 = json.loads(handle_open({"url": "about:blank"}))
    assert open_res2.get("preview_url") == preview_url

    # 4. cobrowse_eval_js via kwargs style
    eval_res = json.loads(handle_eval_js(
        script="document.body.innerHTML = '<h1>Hermes Test</h1><button id=\"btn-ok\">OK</button>'; document.title = 'Hermes Live';"
    ))
    assert eval_res.get("status") == "evaluated"
    print("  [✓] cobrowse_eval_js (kwargs style) executed successfully.")

    # 5. cobrowse_get_state
    state_res = json.loads(handle_get_state({"include_dom_tree": True}))
    assert state_res.get("title") == "Hermes Live"
    assert state_res.get("interactive_elements_count") >= 1
    print("  [✓] cobrowse_get_state verified.")

    # 6. cobrowse_click + graceful error on invalid selector
    click_res = json.loads(handle_click({"selector": "#btn-ok"}))
    assert click_res.get("status") == "clicked"

    err_res = json.loads(handle_navigate({}))
    assert err_res.get("status") == "error"
    print("  [✓] cobrowse_click and structured JSON error handling verified.")

    # 7. cobrowse_scroll
    scroll_res = json.loads(handle_scroll({"direction": "down", "amount": 200}))
    assert scroll_res.get("status") == "scrolled"

    # 8. cobrowse_close and clean re-open
    close_res = json.loads(handle_close({}))
    assert close_res.get("status") == "closed"

    reopen_res = json.loads(handle_open({"url": "about:blank"}))
    assert reopen_res.get("status") == "started"
    assert json.loads(handle_close({})).get("status") == "closed"
    print("  [✓] Clean shutdown and full re-open cycle verified.")

    print("\n[SUCCESS] All Hermes tool, server, and portal tests passed!")


if __name__ == "__main__":
    test_registration_and_manifest()
    test_demo_portal_routes()
    test_tool_invocations_and_server()
