"""True End-to-End Integration Test for Hermes CoBrowse.

Tests the complete live flow without any mocking:
1. Real Demo Portal (uvicorn HTTP server) with login, 2FA OTP gate, and dashboard
2. Real CoBrowseManager (headless Chromium + CDP screencast + FastAPI/WS server)
3. Real WebSocket client acting as the Spatial Canvas Studio UI:
   - Receives init & JPEG frames
   - Observes agent_action & page_updated telemetry
   - Receives intervention_required when 2FA gate is reached
   - Sends real mouse clicks and keyboard keystrokes over WebSocket to solve 2FA
   - Sends resume_agent over WebSocket
4. Agent resumes, clicks Deploy, and verifies the live DOM outcome
"""

import json
import os
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uvicorn
import websocket

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from demo_portal.app import portal_app
from hermes_cobrowse.server import find_free_port
from hermes_cobrowse.tools import (
    CoBrowseManager,
    handle_click,
    handle_close,
    handle_eval_js,
    handle_get_state,
    handle_request_handoff,
    handle_type,
)


def _wait_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def run_e2e():
    print("[*] Starting full end-to-end CoBrowse verification...")

    # 1. Start real demo portal
    portal_port = find_free_port(5050)
    portal_config = uvicorn.Config(app=portal_app, host="127.0.0.1", port=portal_port, log_level="warning")
    portal_server = uvicorn.Server(portal_config)
    portal_thread = threading.Thread(target=portal_server.run, daemon=True)
    portal_thread.start()
    assert _wait_port(portal_port, timeout=5.0), f"Demo portal did not start on port {portal_port}"
    print(f"  [✓] Real Demo Portal running at http://127.0.0.1:{portal_port}")

    mgr = CoBrowseManager.get_instance()
    mgr.set_allow_private_networks(True)
    ws = None

    try:
        # 2. Boot CoBrowse on /login
        login_url = f"http://127.0.0.1:{portal_port}/login"
        start_res = mgr.ensure_started(initial_url=login_url)
        preview_url = start_res["preview_url"]
        parts = urlsplit(preview_url)
        base_http = f"{parts.scheme}://{parts.netloc}"
        token = parse_qs(parts.query).get("token", [""])[0]
        assert token, f"Missing token in preview_url: {preview_url}"
        print(f"  [✓] CoBrowse started at {base_http}/?token=***")

        # 3. Connect real WebSocket client (simulating Spatial Canvas Studio UI)
        ws_url = f"{base_http.replace('http://', 'ws://')}/ws/stream?token={token}"
        ws = websocket.create_connection(ws_url, origin=base_http, timeout=10)

        received_events = []
        frames_seen = 0
        intervention_seen = threading.Event()
        ws_stop = threading.Event()

        def ws_reader():
            nonlocal frames_seen
            ws.settimeout(0.5)
            while not ws_stop.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception:
                    break
                if not raw:
                    break
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "frame":
                    frames_seen += 1
                else:
                    received_events.append(msg)
                    if mtype == "intervention_required":
                        intervention_seen.set()

        reader_thread = threading.Thread(target=ws_reader, daemon=True)
        reader_thread.start()

        # 4. Agent fills login & clicks Sign In
        type_email = json.loads(handle_type({"selector": "#email", "text": "admin@nousresearch.com", "clear_first": True}))
        assert type_email["status"] == "typed"
        type_pw = json.loads(handle_type({"selector": "#password", "text": "hermes-demo-2026", "clear_first": True}))
        assert type_pw["status"] == "typed"
        click_login = json.loads(handle_click({"selector": "#btnLogin"}))
        assert click_login["status"] == "clicked"

        # Wait for redirect to /2fa
        deadline = time.monotonic() + 5.0
        state = {}
        while time.monotonic() < deadline:
            state = json.loads(handle_get_state({"include_dom_tree": True}))
            if "/2fa" in (state.get("url") or ""):
                break
            time.sleep(0.1)
        assert "/2fa" in (state.get("url") or ""), f"Expected /2fa URL, got {state}"
        print("  [✓] Agent login submitted and redirected to /2fa")

        # Locate #otpCode and #btnVerify bounding boxes from DOM state (just like user clicking canvas)
        elements_by_id = {el.get("id"): el for el in state.get("elements", [])}
        otp_box = elements_by_id["otpCode"]["box"]
        verify_box = elements_by_id["btnVerify"]["box"]
        otp_x = otp_box["x"] + otp_box["width"] // 2
        otp_y = otp_box["y"] + otp_box["height"] // 2
        verify_x = verify_box["x"] + verify_box["width"] // 2
        verify_y = verify_box["y"] + verify_box["height"] // 2

        # 5. Agent requests human handoff (blocking call in worker thread)
        handoff_result = {}

        def run_handoff():
            handoff_result["data"] = json.loads(
                handle_request_handoff({
                    "reason": "2FA OTP Gate",
                    "guidance": "Enter 849201 and click Verify",
                    "timeout_seconds": 15,
                })
            )

        handoff_thread = threading.Thread(target=run_handoff, daemon=True)
        handoff_thread.start()

        assert intervention_seen.wait(timeout=5.0), "WebSocket UI never received intervention_required"
        print("  [✓] WebSocket UI received intervention_required alert")

        # 6. Simulate real human interacting over WebSocket canvas to solve 2FA
        ws.send(json.dumps({"type": "mouse_down", "x": otp_x, "y": otp_y, "button": "left"}))
        ws.send(json.dumps({"type": "mouse_up", "x": otp_x, "y": otp_y, "button": "left"}))
        time.sleep(0.1)

        for ch in "849201":
            ws.send(json.dumps({"type": "key_down", "key": ch, "code": f"Digit{ch}"}))
            ws.send(json.dumps({"type": "key_up", "key": ch, "code": f"Digit{ch}"}))
        time.sleep(0.1)

        ws.send(json.dumps({"type": "mouse_down", "x": verify_x, "y": verify_y, "button": "left"}))
        ws.send(json.dumps({"type": "mouse_up", "x": verify_x, "y": verify_y, "button": "left"}))

        # Wait until browser reaches /dashboard from human canvas interaction
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            cur_state = json.loads(handle_get_state({"include_dom_tree": False}))
            if "/dashboard" in (cur_state.get("url") or ""):
                break
            time.sleep(0.1)
        assert "/dashboard" in (cur_state.get("url") or ""), f"Human WS interaction did not reach /dashboard: {cur_state}"
        print("  [✓] Human pointer & keyboard events over WebSocket passed 2FA and reached /dashboard")

        # User clicks Resume Hermes Agent
        ws.send(json.dumps({"type": "resume_agent", "note": "2FA solved by user"}))
        handoff_thread.join(timeout=5.0)
        assert handoff_result.get("data", {}).get("status") == "resolved", f"Handoff failed: {handoff_result}"
        assert "/dashboard" in (handoff_result["data"].get("current_url") or "")
        print("  [✓] Agent cleanly resumed after WebSocket resume_agent signal")

        # 7. Agent completes deployment on dashboard
        deploy_click = json.loads(handle_click({"selector": "#btnDeploy"}))
        assert deploy_click["status"] == "clicked"
        eval_status = json.loads(handle_eval_js({
            "script": "document.getElementById('deployStatus').style.display + '::' + document.getElementById('deployStatus').innerText"
        }))
        assert "block::" in eval_status["result"] and "SUCCESS" in eval_status["result"], f"Unexpected deploy status: {eval_status}"
        print(f"  [✓] Deployment verified: {eval_status['result'].split('::', 1)[1].strip()}")

        ws_stop.set()
        reader_thread.join(timeout=2.0)
        assert frames_seen >= 1, f"Expected >= 1 live frames over WebSocket, got {frames_seen}"
        assert any(e.get("type") == "page_updated" and "/2fa" in (e.get("url") or "") for e in received_events), "Missing /2fa page_updated sync"
        assert any(e.get("type") == "page_updated" and "/dashboard" in (e.get("url") or "") for e in received_events), "Missing /dashboard page_updated sync"
        print(f"  [✓] Telemetry verified: {frames_seen} live JPEG frames & real-time URL sync events received")

    finally:
        if "ws_stop" in locals():
            ws_stop.set()
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if "reader_thread" in locals():
            reader_thread.join(timeout=2.0)
        handle_close({})
        mgr.set_allow_private_networks(False)
        portal_server.should_exit = True
        print("  [✓] Clean shutdown of CoBrowse & Demo Portal")

    print("\n[SUCCESS] Full End-to-End Co-Browsing & 2FA Handoff test PASSED!")


if __name__ == "__main__":
    run_e2e()
