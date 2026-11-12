"""Unit & Integration tests for Hermes Tool Handlers, Server, Portal, and Registration."""

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit
import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient
from demo_portal.app import portal_app
from hermes_cobrowse import register
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
)


import websocket


def _parse_preview_url(preview_url: str):
    parts = urlsplit(preview_url)
    base_http = f"{parts.scheme}://{parts.netloc}"
    token = parse_qs(parts.query).get("token", [""])[0]
    assert token, f"Missing auth token in preview_url: {preview_url}"
    return base_http, token


def _verify_websocket_security_and_stream(preview_url: str):
    base_http, token = _parse_preview_url(preview_url)
    ws_base = base_http.replace("http://", "ws://")

    # 1. Missing token rejected
    for bad_ws in (
        f"{ws_base}/ws/stream",
        f"{ws_base}/ws/stream?token=invalid-token",
        f"{ws_base}/ws/studio?token=invalid-token",
    ):
        rejected = False
        try:
            c = websocket.create_connection(bad_ws, origin=base_http, timeout=3)
            c.close()
        except Exception:
            rejected = True
        assert rejected, f"Unauthenticated WS should be rejected: {bad_ws}"

    # 2. Valid token with evil/missing Origin rejected
    for bad_origin in (None, "https://evil.example"):
        rejected = False
        try:
            kw = {"origin": bad_origin} if bad_origin else {"suppress_origin": True}
            c = websocket.create_connection(f"{ws_base}/ws/stream?token={token}", timeout=3, **kw)
            c.close()
        except Exception:
            rejected = True
        assert rejected, f"Disallowed Origin should be rejected: {bad_origin!r}"

    # 3. Valid token + valid localhost Origin succeeds
    ws = websocket.create_connection(f"{ws_base}/ws/stream?token={token}", origin=base_http, timeout=5)
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
    print("  [✓] WebSocket token + Origin checks, init, JPEG frame, and takeover_toggle verified.")


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
    assert manifest["config_schema"]["allow_private_networks"]["default"] is False

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
    print("[*] Testing Tool Handlers, Auth/Host/Origin security, Server endpoints, and lifecycle...")

    # 1. cobrowse_open
    open_res = json.loads(handle_open({"url": "about:blank", "viewport_width": 1024, "viewport_height": 768}))
    assert open_res.get("status") in ["started", "already_running"]
    preview_url = open_res.get("preview_url")
    assert preview_url and preview_url.startswith("http://127.0.0.1:")
    base_http, token = _parse_preview_url(preview_url)
    print(f"  [✓] cobrowse_open succeeded with tokenized preview_url: {base_http}/?token=***")

    # 2. Verify unauthenticated HTTP requests & bad Host headers are rejected
    for unauth_url in (f"{base_http}/health", f"{base_http}/api/sessions", f"{base_http}/api/state"):
        got_401 = False
        try:
            urllib.request.urlopen(unauth_url, timeout=3)
        except urllib.error.HTTPError as e:
            got_401 = (e.code == 401)
        assert got_401, f"Expected 401 without token on {unauth_url}"

    bad_host_req = urllib.request.Request(
        f"{base_http}/health?token={token}",
        headers={"Host": "evil.example:9878"},
    )
    got_400 = False
    try:
        urllib.request.urlopen(bad_host_req, timeout=3)
    except urllib.error.HTTPError as e:
        got_400 = (e.code == 400)
    assert got_400, "Expected 400 Invalid host header for DNS rebinding test"

    # Authenticated HTTP endpoints work cleanly
    with urllib.request.urlopen(urllib.request.Request(f"{base_http}/health", headers={"X-CoBrowse-Token": token}), timeout=3) as resp:
        health = json.loads(resp.read().decode("utf-8"))
        assert health["status"] == "healthy"
        assert health["running"] is True

    req = urllib.request.Request(
        f"{base_http}/api/resume",
        data=b"",
        headers={"X-CoBrowse-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3) as resp:
        resume_json = json.loads(resp.read().decode("utf-8"))
        assert resume_json["status"] == "ok"
    print("  [✓] Token auth, DNS-rebinding Host allowlist, /health, and /api/resume verified.")

    _verify_websocket_security_and_stream(preview_url)

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

    # 6. cobrowse_click + SSRF / invalid navigate rejection
    click_res = json.loads(handle_click({"selector": "#btn-ok"}))
    assert click_res.get("status") == "clicked"

    err_res = json.loads(handle_navigate({}))
    assert err_res.get("status") == "error"
    for blocked in ("file:///etc/passwd", "http://169.254.169.254/latest/meta-data/", "javascript:alert(1)"):
        ssrf_res = json.loads(handle_navigate({"url": blocked}))
        assert ssrf_res.get("status") == "error", f"SSRF not blocked for {blocked}: {ssrf_res}"
    print("  [✓] cobrowse_click, structured JSON errors, and SSRF navigate refusal verified.")

    # 7. cobrowse_scroll
    scroll_res = json.loads(handle_scroll({"direction": "down", "amount": 200}))
    assert scroll_res.get("status") == "scrolled"

    # 7b. cobrowse_request_handoff aborts immediately during gateway OR cron turns EVEN IF a viewer is connected
    ws_conn = websocket.create_connection(
        f"{base_http.replace('http://', 'ws://')}/ws/studio?token={token}",
        origin=base_http,
        timeout=5,
    )
    try:
        ws_conn.recv()  # studio_init

        # Gateway platform context
        os.environ["HERMES_SESSION_PLATFORM"] = "telegram"
        try:
            gw_res = json.loads(handle_request_handoff({
                "reason": "Gateway 2FA gate",
                "timeout_seconds": 30,
            }))
            assert gw_res.get("status") == "unattended_abort", gw_res
            assert "gateway" in (gw_res.get("source") or "")
        finally:
            os.environ.pop("HERMES_SESSION_PLATFORM", None)

        # Cron execution identity (if cron.execution_identity is installed in runtime)
        try:
            import cron.execution_identity as cei  # type: ignore

            tok = cei._CURRENT.set(object())
            try:
                cron_res = json.loads(handle_request_handoff({
                    "reason": "Cron 2FA gate",
                    "timeout_seconds": 30,
                }))
                assert cron_res.get("status") == "unattended_abort", cron_res
                assert cron_res.get("source") == "cron"
            finally:
                cei._CURRENT.reset(tok)
        except ImportError:
            pass
    finally:
        ws_conn.close()
    print("  [✓] Gateway & cron unattended 2FA abort verified even with active WS viewer.")

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
