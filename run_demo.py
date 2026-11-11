"""Hermes CoBrowse — End-to-End Live Demonstration Runner.

Spins up:
1. The secure demo portal (with 2FA gate)
2. CoBrowse CDP streaming server
3. Opens the CoBrowse Live Canvas in your browser
4. Executes autonomous Hermes Agent workflow:
   - Navigates to portal
   - Fills credentials
   - Hits 2FA gate
   - Triggers Human Intervention Handoff (sounds alert chime, shows banner)
   - Waits for human verification
   - Resumes Hermes
   - Deploys cloud cluster
"""

import json
import os
import socket
import sys
import threading
import time
import webbrowser
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from demo_portal.app import portal_app
from hermes_cobrowse.server import find_free_port
from hermes_cobrowse.tools import (
    CoBrowseManager,
    handle_click,
    handle_close,
    handle_get_state,
    handle_request_handoff,
    handle_type,
)


def start_portal_server(port: int):
    config = uvicorn.Config(app=portal_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run()


def wait_for_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def main():
    print("=" * 70)
    print("   HERMES COBROWSE — LIVE CO-BROWSING DEMO RUNNER")
    print("   Dual-Control Co-Browsing & Autonomous 2FA Handoff")
    print("=" * 70)

    # 1. Start Demo Portal in background thread
    portal_port = find_free_port(5050)
    print(f"\n[1/5] Launching Secure Demo Portal on http://127.0.0.1:{portal_port}...")
    t_portal = threading.Thread(target=start_portal_server, args=(portal_port,), daemon=True)
    t_portal.start()
    if not wait_for_port(portal_port, timeout=5.0):
        raise RuntimeError(f"Demo portal failed to listen on port {portal_port}")

    # 2. Boot CoBrowse Engine
    print("[2/5] Initializing CoBrowse CDP Engine & Live Streaming Server...")
    mgr = CoBrowseManager.get_instance()
    portal_url = f"http://127.0.0.1:{portal_port}/login"
    mgr.ensure_started(initial_url=portal_url)

    preview_url = mgr.preview_url
    print(f"[+] CoBrowse Live Canvas active at: {preview_url}")

    # 3. Open Canvas in Browser (simulating Hermes Desktop preview pane)
    print("[3/5] Opening Hermes Desktop preview pane in default browser...")
    webbrowser.open(preview_url)
    time.sleep(2.5)

    # 4. Hermes Autonomous Agent Steps
    print("\n[4/5] Hermes Agent begins autonomous workflow:")
    print("  -> Step 1: Inspecting login page DOM...")
    state = json.loads(handle_get_state({"include_dom_tree": True}))
    print(f"     Found page title: '{state.get('title')}' with {state.get('interactive_elements_count')} elements.")
    time.sleep(1)

    print("  -> Step 2: Hermes enters email 'admin@nousresearch.com'...")
    handle_type({"selector": "#email", "text": "admin@nousresearch.com", "clear_first": True})
    time.sleep(0.8)

    print("  -> Step 3: Hermes enters password 'hermes-demo-2026'...")
    handle_type({"selector": "#password", "text": "hermes-demo-2026", "clear_first": True})
    time.sleep(0.8)

    print("  -> Step 4: Hermes clicks 'Sign In with Credentials'...")
    handle_click({"selector": "#btnLogin"})
    time.sleep(1.5)

    # Check state after sign in
    state = json.loads(handle_get_state({"include_dom_tree": True}))
    print(f"     Current page title: '{state.get('title')}'")

    if "Two-Factor" in state.get("title", "") or "Security" in state.get("title", ""):
        print("\n" + "!" * 70)
        print("  [!] 2FA GATE DETECTED! Hermes requests Human Intervention Handoff.")
        print("  [!] LOOK AT YOUR BROWSER CANVAS: You will hear the alert chime,")
        print("      see the amber warning banner, and can interact directly!")
        print("!" * 70)

        # Hermes triggers the human handoff tool call
        print("\nWaiting for user on the canvas to enter OTP '849201' and click Resume...")
        print("(Note: You can click the canvas directly, type 849201, and click 'Resume Hermes Agent')\n")

        # In case the user is running unattended, auto-fill after 10 seconds
        def auto_assist():
            time.sleep(10)
            if mgr.engine and mgr.engine.intervention_active:
                print("\n[*] [Auto-Assist Mode]: Simulating user entering OTP on canvas...")
                mgr.run_async(mgr.engine.click(selector="#otpCode"))
                mgr.run_async(mgr.engine.type_text("849201", selector="#otpCode"))
                time.sleep(1)
                mgr.run_async(mgr.engine.click(selector="#btnVerify"))
                time.sleep(1.5)
                mgr.engine.resolve_human_intervention("Auto-assisted 2FA completion")

        assist_thread = threading.Thread(target=auto_assist, daemon=True)
        assist_thread.start()

        handoff_res = handle_request_handoff({
            "reason": "Two-Factor Authentication Required (Apex Hardware Token)",
            "guidance": "Please enter the 6-digit security code '849201' in the input field and click 'Verify & Confirm Identity'. Then click 'Resume Hermes Agent'.",
            "timeout_seconds": 60,
        })
        print(f"     Handoff result: {handoff_res}")

    # 5. Autonomous completion on Dashboard
    print("\n[5/5] Hermes Agent resumes autonomous execution on authenticated dashboard:")
    time.sleep(1.5)
    dash_state = json.loads(handle_get_state({"include_dom_tree": True}))
    print(f"     Dashboard title: '{dash_state.get('title')}'")

    print("  -> Step 5: Hermes locates 'Deploy Hermes Cluster' button and clicks...")
    handle_click({"selector": "#btnDeploy"})
    time.sleep(1.5)

    print("\n" + "=" * 70)
    print("  [✓] MISSION ACCOMPLISHED!")
    print("      Hermes successfully navigated, securely handed off 2FA to user,")
    print("      resumed execution, and deployed the production cluster.")
    print("=" * 70)
    print("\nThe CoBrowse preview server remains running for manual inspection.")
    print(f"Preview URL: {preview_url}")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping demo...")
        handle_close({})
        print("Done.")


if __name__ == "__main__":
    main()
