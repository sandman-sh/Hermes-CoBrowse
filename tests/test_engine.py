"""Integration tests for Hermes CoBrowse BrowserEngine (Zero dependencies)."""

import asyncio
import os
import sys
import threading
import time

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_cobrowse.engine import BrowserEngine


async def run_tests():
    print("[*] Starting BrowserEngine integration tests...")
    engine = BrowserEngine(width=1024, height=768)

    frames_received = []
    events_received = []

    def on_frame(data, metadata):
        frames_received.append(data)

    def on_event(evt):
        events_received.append(evt)

    engine.add_frame_listener(on_frame)
    engine.add_event_listener(on_event)

    # 1. Start browser
    try:
        start_info = await engine.start(initial_url="about:blank")
        assert start_info["status"] == "started", f"Start failed: {start_info}"
        assert engine.is_running is True
        print("  [✓] Browser started and CDP session attached.")

        # 2. Set test content including React-style colon ID and leading-whitespace class
        await engine.page.set_content("""
            <!DOCTYPE html>
            <html>
            <head><title>Test Page</title></head>
            <body>
                <h1 id="header">Initial Text</h1>
                <input id="test-input" type="text" placeholder="Type here" />
                <button id="test-btn" onclick="document.getElementById('header').innerText = 'Clicked Successfully!'">Click Me</button>
                <button id="react:r0:" class="  special-btn  primary" onclick="document.getElementById('header').innerText = 'Escaped Selector Worked!'">Escaped</button>
            </body>
            </html>
        """)

        # Let CDP screencast emit frames
        await asyncio.sleep(0.6)
        assert len(frames_received) > 0, "Expected at least 1 CDP frame"
        print(f"  [✓] Screencast/snapshot frames received via CDP: {len(frames_received)}")

        # 3. Test State Extraction & CSS.escape selector generation
        state = await engine.get_state(include_dom_tree=True)
        assert state["title"] == "Test Page"
        assert state["interactive_elements_count"] >= 3
        selectors = [el["selector"] for el in state["elements"]]
        escaped_sel = next((s for s in selectors if "react" in s), None)
        assert escaped_sel is not None, f"Escaped selector missing: {selectors}"
        print(f"  [✓] State extracted: {state['interactive_elements_count']} elements (escaped selector: {escaped_sel}).")

        # 4. Test Typing with default selector clear, explicit clear_first=False, and clear_first=True
        await engine.type_text("Old Value", selector="#test-input")
        await engine.type_text("Replaced", selector="#test-input")
        val_rep = await engine.page.locator("#test-input").input_value()
        assert val_rep == "Replaced", f"Expected default replace 'Replaced', got {val_rep!r}"
        await engine.type_text(" Text", selector="#test-input", clear_first=False)
        val_app = await engine.page.locator("#test-input").input_value()
        assert val_app == "Replaced Text", f"Expected append 'Replaced Text', got {val_app!r}"
        type_res = await engine.type_text("Hello Hermes", selector="#test-input", clear_first=True)
        assert type_res["status"] == "typed"
        val = await engine.page.locator("#test-input").input_value()
        assert val == "Hello Hermes", f"Expected 'Hello Hermes', got {val!r}"
        print(f"  [✓] Typing default-replace, append, & explicit clear_first verified: '{val}'")

        # 5. Test Clicking with normal and escaped selector
        click_res = await engine.click(selector="#test-btn")
        assert click_res["status"] == "clicked"
        header_text = await engine.page.locator("#header").inner_text()
        assert header_text == "Clicked Successfully!"

        click_escaped = await engine.click(selector=escaped_sel)
        assert click_escaped["status"] == "clicked"
        header_text2 = await engine.page.locator("#header").inner_text()
        assert header_text2 == "Escaped Selector Worked!", f"Escaped click failed: {header_text2}"
        print("  [✓] Clicking verified (standard & CSS-escaped selectors).")

        # 6. Test dispatch_user_input with unsupported special keys & Spacebar
        for special_key in ["Dead", "Unidentified", "OS", "Process", "Spacebar"]:
            await engine.dispatch_user_input({"type": "key_down", "key": special_key})
            await engine.dispatch_user_input({"type": "key_up", "key": special_key})
        print("  [✓] Unsupported/special key sanitization verified.")

        # 7. Test thread-safe resolve_human_intervention from an external thread
        def external_resolver():
            time.sleep(0.3)
            engine.resolve_human_intervention("Resolved from external thread")

        t = threading.Thread(target=external_resolver, daemon=True)
        t.start()
        handoff_res = await engine.request_human_intervention(
            reason="Test 2FA",
            guidance="Enter OTP",
            timeout_seconds=5,
        )
        assert handoff_res["status"] == "resolved", f"Expected resolved, got {handoff_res}"
        assert any(e.get("type") == "intervention_resolved" for e in events_received)
        print("  [✓] Thread-safe human intervention handoff & resolution verified.")
    finally:
        # 8. Test Shutdown
        await engine.close()
        assert engine.is_running is False
        print("  [✓] Browser engine closed cleanly.")

    print("\n[SUCCESS] All BrowserEngine integration tests passed with 100% accuracy!")


if __name__ == "__main__":
    asyncio.run(run_tests())
