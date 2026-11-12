"""Comprehensive Real Multi-Window Spatial Studio Integration Test (Zero Mocks).

Tests concurrent live Chromium windows, independent session routing, Back/Forward/Reload,
dynamic viewport resizing, real Chromium Console & HTTP Network capture, PNG screenshot export,
DOM element highlighting, /ws/studio multiplexing, and independent window teardown.
"""

import json
import os
import socket
import sys
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlsplit
import uvicorn
import websocket

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from demo_portal.app import portal_app
from hermes_cobrowse.server import find_free_port
from hermes_cobrowse.tools import (
    CoBrowseManager,
    handle_close,
    handle_eval_js,
    handle_open,
)


def _wait_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def _http_json(url: str, method: str = "GET", body: dict = None, token: str = "") -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-CoBrowse-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _split_preview(preview_url: str):
    parts = urlsplit(preview_url)
    base_http = f"{parts.scheme}://{parts.netloc}"
    token = parse_qs(parts.query).get("token", [""])[0]
    assert token, f"Missing token in preview_url: {preview_url}"
    return base_http, token


def run_studio_tests():
    print("[*] Starting Multi-Window Spatial Studio E2E test...")

    portal_port = find_free_port(18050)
    portal_config = uvicorn.Config(app=portal_app, host="127.0.0.1", port=portal_port, log_level="warning")
    portal_server = uvicorn.Server(portal_config)
    portal_thread = threading.Thread(target=portal_server.run, daemon=True)
    portal_thread.start()
    assert _wait_port(portal_port, timeout=5.0), f"Demo portal failed on port {portal_port}"
    base_portal = f"http://127.0.0.1:{portal_port}"
    print(f"  [✓] Real Demo Portal running at {base_portal}")

    ws = None
    mgr = CoBrowseManager.get_instance()
    mgr.set_allow_private_networks(True)
    try:
        # 1. Launch two concurrent independent sessions via Hermes tools
        r1 = json.loads(handle_open({"url": f"{base_portal}/", "session_id": "win-alpha", "title": "Alpha Portal"}))
        assert r1["session_id"] == "win-alpha"
        preview_url = r1["preview_url"]
        base_http, token = _split_preview(preview_url)

        r2 = json.loads(handle_open({
            "url": f"{base_portal}/login",
            "session_id": "win-beta",
            "title": "Beta Login",
            "viewport_width": 820,
            "viewport_height": 1024,
        }))
        assert r2["session_id"] == "win-beta"
        assert r2["total_sessions"] == 2
        print("  [✓] Two independent headless Chromium sessions running concurrently.")

        # 2. Launch a third window via REST POST /api/sessions (what Canvas UI "+ New Window" calls)
        created = _http_json(
            f"{base_http}/api/sessions",
            method="POST",
            body={"url": f"{base_portal}/dashboard", "title": "Gamma Dashboard", "width": 390, "height": 844},
            token=token,
        )
        sid_gamma = created["session_id"]
        assert sid_gamma and sid_gamma not in ("win-alpha", "win-beta")

        listed = _http_json(f"{base_http}/api/sessions", token=token)
        ids = [s["session_id"] for s in listed["sessions"]]
        assert set(ids) == {"win-alpha", "win-beta", sid_gamma}, f"Unexpected session list: {ids}"
        print(f"  [✓] REST POST /api/sessions spawned third live window ({sid_gamma}); total=3.")

        # 3. Verify multiplexed /ws/studio WebSocket with active reader thread
        ws_url = f"{base_http.replace('http://', 'ws://')}/ws/studio?token={token}"
        ws = websocket.create_connection(ws_url, origin=base_http, timeout=10)
        studio_init = json.loads(ws.recv())
        assert studio_init["type"] == "studio_init"
        assert len(studio_init["sessions"]) == 3

        frames_by_sid = set()
        events_seen = []
        ws_stop = threading.Event()

        def _reader():
            ws.settimeout(0.4)
            while not ws_stop.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception:
                    break
                if not raw:
                    break
                pkt = json.loads(raw)
                if pkt.get("type") == "frame":
                    frames_by_sid.add(pkt.get("session_id"))
                else:
                    events_seen.append(pkt)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(frames_by_sid) < 2:
            time.sleep(0.05)
        assert len(frames_by_sid) >= 2, f"Expected frames from multiple sessions, got {frames_by_sid}"
        print(f"  [✓] /ws/studio streamed live frames for sessions: {sorted(frames_by_sid)}")

        # 4. Verify Back / Forward / Reload navigation on win-alpha
        _http_json(f"{base_http}/api/sessions/win-alpha/navigate", method="POST", body={"url": f"{base_portal}/2fa"}, token=token)
        back_res = _http_json(f"{base_http}/api/sessions/win-alpha/back", method="POST", token=token)
        assert back_res["status"] == "navigated_back"
        assert back_res["url"].rstrip("/") == base_portal
        fwd_res = _http_json(f"{base_http}/api/sessions/win-alpha/forward", method="POST", token=token)
        assert fwd_res["status"] == "navigated_forward"
        assert "/2fa" in fwd_res["url"]
        rel_res = _http_json(f"{base_http}/api/sessions/win-alpha/reload", method="POST", token=token)
        assert rel_res["status"] == "reloaded"
        print("  [✓] Real Playwright Back / Forward / Reload history verified.")

        # 5. Verify Dynamic Viewport Resize on win-beta
        resize_res = _http_json(
            f"{base_http}/api/sessions/win-beta/resize",
            method="POST",
            body={"width": 412, "height": 732},
            token=token,
        )
        assert resize_res["viewport"] == {"width": 412, "height": 732}
        dims = json.loads(handle_eval_js({
            "session_id": "win-beta",
            "script": "window.innerWidth + 'x' + window.innerHeight",
        }))
        assert dims["result"] == "412x732", f"Viewport did not reflow inside Chromium: {dims}"
        print("  [✓] Dynamic Chromium viewport resize (412x732) verified.")

        # 6. Verify Real Console & Network Telemetry Capture (zero fake telemetry!)
        handle_eval_js({"session_id": "win-alpha", "script": "console.warn('real-warning-probe');"})
        time.sleep(0.15)
        telem = _http_json(f"{base_http}/api/sessions/win-alpha/telemetry", token=token)
        assert any("real-warning-probe" in c.get("text", "") for c in telem["console_logs"]), "Missing console capture"
        assert any("/2fa" in n.get("url", "") and n.get("status") == 200 for n in telem["network_logs"]), "Missing HTTP 200 capture"
        print(f"  [✓] Real Console ({len(telem['console_logs'])}) & HTTP Network ({len(telem['network_logs'])}) telemetry verified.")

        # 7. Verify Real DOM Element Highlight & PNG Screenshot
        hl = _http_json(f"{base_http}/api/sessions/win-alpha/highlight", method="POST", body={"selector": "#otpCode"}, token=token)
        assert hl["status"] == "highlighted" and hl["box"]["width"] > 0

        shot_req = urllib.request.Request(
            f"{base_http}/api/sessions/win-alpha/screenshot",
            headers={"X-CoBrowse-Token": token},
        )
        with urllib.request.urlopen(shot_req, timeout=5) as resp:
            png_bytes = resp.read()
            assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n"), "Invalid PNG header"
        print(f"  [✓] Live DOM element highlight & PNG screenshot export ({len(png_bytes)} bytes) verified.")

        # 8. Verify single-window DELETE /api/sessions/{sid} leaves siblings alive
        del_res = _http_json(f"{base_http}/api/sessions/{sid_gamma}", method="DELETE", token=token)
        assert del_res["status"] == "closed"
        time.sleep(0.15)
        after_list = _http_json(f"{base_http}/api/sessions", token=token)
        after_ids = [s["session_id"] for s in after_list["sessions"]]
        assert set(after_ids) == {"win-alpha", "win-beta"}, f"Sibling windows should remain alive: {after_ids}"
        assert any(e.get("type") == "console_log" for e in events_seen), "Missing console_log WS broadcast"
        assert any(e.get("type") == "network_log" for e in events_seen), "Missing network_log WS broadcast"
        assert any(e.get("type") == "viewport_updated" for e in events_seen), "Missing viewport_updated WS broadcast"
        assert any(e.get("type") == "session_closed" and e.get("session_id") == sid_gamma for e in events_seen), "Missing session_closed WS broadcast"
        print("  [✓] Per-window close isolated & live WS telemetry broadcasts verified.")

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
        print("  [✓] Clean full Studio & Portal shutdown.")

    print("\n[SUCCESS] All Multi-Window Spatial Canvas Studio E2E tests PASSED!")


if __name__ == "__main__":
    run_studio_tests()
