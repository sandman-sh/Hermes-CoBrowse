"""Browser Engine for Hermes CoBrowse.

Manages headless Chromium via Playwright, streams frames using Chrome DevTools Protocol (CDP),
arbitrates dual-control between human and Hermes Agent, and records visual telemetry.
"""

from __future__ import annotations

import asyncio
from collections import deque
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Deque, Dict, List, Optional

from hermes_cobrowse.security import (
    build_minimal_browser_env,
    is_route_request_allowed,
    validate_target_url_async,
)

if TYPE_CHECKING:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        CDPSession,
        ConsoleMessage,
        Frame,
        Page,
        Request,
        Response,
    )

logger = logging.getLogger("cobrowse.engine")

_UNSUPPORTED_PLAYWRIGHT_KEYS = {
    "Dead",
    "Unidentified",
    "OS",
    "Process",
    "GroupNext",
    "Compose",
    "ModeChange",
}


class BrowserEngine:
    """Manages browser lifecycle, CDP screencasting, and bidirectional input arbitration."""

    def __init__(
        self,
        width: int = 1280,
        height: int = 800,
        session_id: str = "default",
        title: str = "",
        allow_private_networks: bool = False,
    ):
        self.width = width
        self.height = height
        self.session_id = session_id or "default"
        self.window_title = title or ""
        self.allow_private_networks = bool(allow_private_networks)
        self.created_at = time.time()

        self.playwright: Optional[Any] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.cdp: Optional[CDPSession] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self.is_running = False
        self.latest_frame_b64: Optional[str] = None
        self.latest_metadata: Dict[str, Any] = {}
        self.current_title: str = title or ""
        self.current_url: str = "about:blank"

        # Callbacks for WebSocket broadcasting
        self.frame_listeners: List[Callable[[str, Dict[str, Any]], Any]] = []
        self.event_listeners: List[Callable[[Dict[str, Any]], Any]] = []

        # Human Handoff / Intervention State
        self.intervention_active = False
        self.intervention_reason: Optional[str] = None
        self.intervention_guidance: Optional[str] = None
        self.intervention_deadline: Optional[float] = None
        self.intervention_event: Optional[asyncio.Event] = None
        self.user_takeover_active = False

        # Action history & real browser telemetry buffers
        self.action_history: Deque[Dict[str, Any]] = deque(maxlen=250)
        self.console_logs: Deque[Dict[str, Any]] = deque(maxlen=200)
        self.network_logs: Deque[Dict[str, Any]] = deque(maxlen=200)
        self._request_start_times: Dict[int, float] = {}
        self._telemetry_seq = 0
        self._last_reported_url: Optional[str] = None
        self._nav_history: List[str] = []
        self._nav_index: int = -1

    def _next_seq(self) -> int:
        self._telemetry_seq += 1
        return self._telemetry_seq

    def _schedule_event(self, event: Dict[str, Any]):
        if self._loop and self._loop.is_running():
            self._loop.create_task(self.broadcast_event(event))

    def add_frame_listener(self, callback: Callable[[str, Dict[str, Any]], Any]):
        self.frame_listeners.append(callback)

    def remove_frame_listener(self, callback: Callable[[str, Dict[str, Any]], Any]):
        if callback in self.frame_listeners:
            self.frame_listeners.remove(callback)

    def add_event_listener(self, callback: Callable[[Dict[str, Any]], Any]):
        self.event_listeners.append(callback)

    def remove_event_listener(self, callback: Callable[[Dict[str, Any]], Any]):
        if callback in self.event_listeners:
            self.event_listeners.remove(callback)

    async def broadcast_event(self, event: Dict[str, Any]):
        """Send an event to all connected UI clients, tagged with session_id."""
        payload = {"session_id": self.session_id, **event}
        if event.get("type") in ("agent_action", "page_updated", "intervention_required", "intervention_resolved", "viewport_updated"):
            self.action_history.append({
                "id": self._next_seq(),
                "ts": time.time(),
                "session_id": self.session_id,
                "type": event.get("type"),
                "action": event.get("action") or event.get("type"),
                "label": event.get("label") or event.get("url") or event.get("reason") or "",
                "url": event.get("url", ""),
            })
        for listener in list(self.event_listeners):
            try:
                if asyncio.iscoroutinefunction(listener):
                    await listener(payload)
                else:
                    listener(payload)
            except Exception as e:
                logger.debug(f"Error broadcasting event: {e}")

    async def capture_frame(self):
        """Capture an immediate snapshot so static pages render without waiting for a repaint."""
        if not self.cdp or not self.is_running:
            return
        try:
            res = await self.cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 82})
            data = res.get("data")
            if data:
                self.latest_frame_b64 = data
                for listener in list(self.frame_listeners):
                    try:
                        if asyncio.iscoroutinefunction(listener):
                            await listener(data, self.latest_metadata)
                        else:
                            listener(data, self.latest_metadata)
                    except Exception as e:
                        logger.debug(f"Error notifying frame listener: {e}")
        except Exception as e:
            logger.debug(f"Snapshot capture skipped: {e}")

    async def capture_screenshot_bytes(self, fmt: str = "png") -> bytes:
        """Capture a raw PNG/JPEG screenshot directly from Chromium."""
        if not self.page or not self.is_running:
            raise RuntimeError("Browser is not running.")
        img_type = "png" if fmt != "jpeg" else "jpeg"
        return await self.page.screenshot(type=img_type, full_page=False)

    async def _on_screencast_frame(self, event: Dict[str, Any]):
        """Handler for CDP Page.screencastFrame events."""
        cdp_sid = event.get("sessionId")
        data = event.get("data")
        metadata = event.get("metadata", {})

        self.latest_frame_b64 = data
        self.latest_metadata = metadata

        # Immediately acknowledge frame to keep the CDP stream flowing smoothly
        if self.cdp and cdp_sid is not None:
            try:
                await self.cdp.send("Page.screencastFrameAck", {"sessionId": cdp_sid})
            except Exception:
                pass

        # Broadcast frame to frontend listeners
        for listener in list(self.frame_listeners):
            try:
                if asyncio.iscoroutinefunction(listener):
                    await listener(data, metadata)
                else:
                    listener(data, metadata)
            except Exception as e:
                logger.debug(f"Error notifying frame listener: {e}")

    def _on_frame_navigated(self, frame: Frame):
        """Sync omnibar URL when links or form redirects change the main frame."""
        if not self.page or frame != self.page.main_frame:
            return
        url = frame.url
        if not url:
            return
        self.current_url = url
        if url == self._last_reported_url:
            return
        self._last_reported_url = url

        if not self._nav_history or self._nav_history[self._nav_index] != url:
            if self._nav_index >= 0 and self._nav_index < len(self._nav_history) - 1:
                self._nav_history = self._nav_history[: self._nav_index + 1]
            self._nav_history.append(url)
            if len(self._nav_history) > 100:
                self._nav_history.pop(0)
            self._nav_index = len(self._nav_history) - 1

        async def _emit_page_update():
            try:
                title = await self.page.title() if self.page else ""
            except Exception:
                title = ""
            self.current_title = title
            await self.broadcast_event({
                "type": "page_updated",
                "url": url,
                "title": title,
                "statusCode": 200,
                "can_go_back": self._nav_index > 0,
                "can_go_forward": 0 <= self._nav_index < len(self._nav_history) - 1,
            })

        if self._loop and self._loop.is_running():
            self._loop.create_task(_emit_page_update())

    def _on_console(self, msg: ConsoleMessage):
        """Capture real Chromium console output."""
        try:
            loc = msg.location or {}
            entry = {
                "id": self._next_seq(),
                "ts": time.time(),
                "level": msg.type or "log",
                "text": msg.text or "",
                "url": str(loc.get("url") or ""),
                "line": int(loc.get("lineNumber") or 0),
            }
            self.console_logs.append(entry)
            self._schedule_event({"type": "console_log", "entry": entry})
        except Exception:
            pass

    def _on_page_error(self, err: Any):
        """Capture uncaught page JS exceptions."""
        try:
            entry = {
                "id": self._next_seq(),
                "ts": time.time(),
                "level": "error",
                "text": str(err),
                "url": self.current_url,
                "line": 0,
            }
            self.console_logs.append(entry)
            self._schedule_event({"type": "console_log", "entry": entry})
        except Exception:
            pass

    def _on_request(self, request: Request):
        try:
            if len(self._request_start_times) > 500:
                self._request_start_times.clear()
            self._request_start_times[id(request)] = time.monotonic()
        except Exception:
            pass

    def _on_response(self, response: Response):
        """Capture real HTTP responses passing through this browser context."""
        try:
            req = response.request
            t0 = self._request_start_times.pop(id(req), None)
            duration_ms = int((time.monotonic() - t0) * 1000) if t0 is not None else None
            entry = {
                "id": self._next_seq(),
                "ts": time.time(),
                "method": req.method,
                "url": response.url,
                "status": response.status,
                "ok": bool(response.ok),
                "resource_type": req.resource_type,
                "duration_ms": duration_ms,
            }
            self.network_logs.append(entry)
            self._schedule_event({"type": "network_log", "entry": entry})
        except Exception:
            pass

    def _on_request_failed(self, request: Request):
        """Capture failed network requests with failure reason."""
        try:
            t0 = self._request_start_times.pop(id(request), None)
            duration_ms = int((time.monotonic() - t0) * 1000) if t0 is not None else None
            entry = {
                "id": self._next_seq(),
                "ts": time.time(),
                "method": request.method,
                "url": request.url,
                "status": 0,
                "ok": False,
                "error": request.failure or "failed",
                "resource_type": request.resource_type,
                "duration_ms": duration_ms,
            }
            self.network_logs.append(entry)
            self._schedule_event({"type": "network_log", "entry": entry})
        except Exception:
            pass

    async def _on_route(self, route: Any):
        """Enforce SSRF/scheme guard on every navigation, redirect hop, and request."""
        req = route.request
        allowed = await is_route_request_allowed(
            req.url,
            req.resource_type,
            req.is_navigation_request(),
            allow_private_networks=self.allow_private_networks,
        )
        if allowed:
            await route.continue_()
        else:
            logger.warning(f"[{self.session_id}] Blocked request by SSRF guard: {req.url}")
            await route.abort("blockedbyclient")

    async def start(self, initial_url: str = "about:blank") -> Dict[str, Any]:
        """Launch Chromium, configure CDP screencast, and navigate to initial_url."""
        if self.is_running and self.page:
            if initial_url and initial_url != "about:blank":
                await self.navigate(initial_url)
            return {
                "status": "already_running",
                "session_id": self.session_id,
                "url": self.page.url,
                "viewport": {"width": self.width, "height": self.height},
            }

        if initial_url and initial_url != "about:blank":
            initial_url = await validate_target_url_async(
                initial_url,
                allow_private_networks=self.allow_private_networks,
                allow_about_blank=True,
            )

        self._loop = asyncio.get_running_loop()
        logger.info(f"[{self.session_id}] Launching Playwright Chromium ({self.width}x{self.height})...")

        try:
            from playwright.async_api import async_playwright

            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                env=build_minimal_browser_env(),
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )

            self.context = await self.browser.new_context(
                viewport={"width": self.width, "height": self.height},
            )
            await self.context.route("**/*", self._on_route)

            self.page = await self.context.new_page()
            self.page.on("framenavigated", self._on_frame_navigated)
            self.page.on("console", self._on_console)
            self.page.on("pageerror", self._on_page_error)
            self.page.on("request", self._on_request)
            self.page.on("response", self._on_response)
            self.page.on("requestfailed", self._on_request_failed)

            # Connect CDP Session for high-FPS screencasting and low-level event mapping
            self.cdp = await self.context.new_cdp_session(self.page)
            self.cdp.on("Page.screencastFrame", self._on_screencast_frame)

            await self.cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 80,
                    "maxWidth": self.width,
                    "maxHeight": self.height,
                    "everyNthFrame": 1,
                },
            )

            self.is_running = True

            if initial_url and initial_url != "about:blank":
                await self.navigate(initial_url)
            else:
                await self.capture_frame()

            return {
                "status": "started",
                "session_id": self.session_id,
                "url": self.page.url,
                "viewport": {"width": self.width, "height": self.height},
            }
        except Exception:
            await self.close()
            raise

    async def navigate(self, url: str) -> Dict[str, Any]:
        """Navigate to a target URL and broadcast telemetry."""
        if not self.page:
            raise RuntimeError("Browser is not running. Call start() first.")

        url = await validate_target_url_async(
            url,
            allow_private_networks=self.allow_private_networks,
            allow_about_blank=True,
        )

        await self.broadcast_event({
            "type": "agent_action",
            "action": "navigate",
            "url": url,
            "label": f"Hermes navigating to {url}",
        })

        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status_code = response.status if response else 200
        except Exception as e:
            if "ERR_BLOCKED_BY_CLIENT" in str(e):
                raise ValueError(f"Navigation blocked by SSRF guard: {e}") from e
            logger.warning(f"Navigation warning: {e}")
            status_code = -1

        title = await self.page.title()
        cur_url = self.page.url
        self._last_reported_url = cur_url
        self.current_url = cur_url
        self.current_title = title
        if not self._nav_history or self._nav_history[self._nav_index] != cur_url:
            if 0 <= self._nav_index < len(self._nav_history) - 1:
                self._nav_history = self._nav_history[: self._nav_index + 1]
            self._nav_history.append(cur_url)
            if len(self._nav_history) > 100:
                self._nav_history.pop(0)
            self._nav_index = len(self._nav_history) - 1

        await self.broadcast_event({
            "type": "page_updated",
            "url": cur_url,
            "title": title,
            "statusCode": status_code,
            "can_go_back": self._nav_index > 0,
            "can_go_forward": 0 <= self._nav_index < len(self._nav_history) - 1,
        })
        await self.capture_frame()

        return {"session_id": self.session_id, "url": cur_url, "title": title, "status": status_code}

    async def go_back(self) -> Dict[str, Any]:
        """Navigate backward in Playwright browser history."""
        if not self.page:
            raise RuntimeError("Browser is not running.")
        resp = await self.page.go_back(wait_until="domcontentloaded", timeout=15000)
        if self._nav_index > 0:
            self._nav_index -= 1
        cur_url = self.page.url
        title = await self.page.title()
        self._last_reported_url = cur_url
        self.current_url = cur_url
        self.current_title = title
        await self.broadcast_event({
            "type": "page_updated",
            "url": cur_url,
            "title": title,
            "statusCode": resp.status if resp else 200,
            "can_go_back": self._nav_index > 0,
            "can_go_forward": 0 <= self._nav_index < len(self._nav_history) - 1,
        })
        await self.capture_frame()
        return {"session_id": self.session_id, "status": "navigated_back" if resp else "no_history", "url": cur_url, "title": title}

    async def go_forward(self) -> Dict[str, Any]:
        """Navigate forward in Playwright browser history."""
        if not self.page:
            raise RuntimeError("Browser is not running.")
        resp = await self.page.go_forward(wait_until="domcontentloaded", timeout=15000)
        if 0 <= self._nav_index < len(self._nav_history) - 1:
            self._nav_index += 1
        cur_url = self.page.url
        title = await self.page.title()
        self._last_reported_url = cur_url
        self.current_url = cur_url
        self.current_title = title
        await self.broadcast_event({
            "type": "page_updated",
            "url": cur_url,
            "title": title,
            "statusCode": resp.status if resp else 200,
            "can_go_back": self._nav_index > 0,
            "can_go_forward": 0 <= self._nav_index < len(self._nav_history) - 1,
        })
        await self.capture_frame()
        return {"session_id": self.session_id, "status": "navigated_forward" if resp else "no_history", "url": cur_url, "title": title}

    async def reload(self) -> Dict[str, Any]:
        """Reload the current page and capture an updated frame."""
        if not self.page:
            raise RuntimeError("Browser is not running.")
        resp = await self.page.reload(wait_until="domcontentloaded", timeout=25000)
        cur_url = self.page.url
        title = await self.page.title()
        self.current_url = cur_url
        self.current_title = title
        await self.broadcast_event({
            "type": "page_updated",
            "url": cur_url,
            "title": title,
            "statusCode": resp.status if resp else 200,
            "can_go_back": self._nav_index > 0,
            "can_go_forward": 0 <= self._nav_index < len(self._nav_history) - 1,
        })
        await self.capture_frame()
        return {"session_id": self.session_id, "status": "reloaded", "url": cur_url, "title": title}

    async def resize_viewport(self, width: int, height: int) -> Dict[str, Any]:
        """Dynamically resize headless Chromium viewport and re-tune CDP screencast."""
        if not self.page:
            raise RuntimeError("Browser is not running.")
        w = max(320, min(2560, int(width)))
        h = max(240, min(1600, int(height)))
        if w == self.width and h == self.height:
            return {"session_id": self.session_id, "status": "unchanged", "viewport": {"width": w, "height": h}}

        self.width = w
        self.height = h
        await self.page.set_viewport_size({"width": w, "height": h})

        if self.cdp and self.is_running:
            try:
                await self.cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await self.cdp.send(
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": 82,
                        "maxWidth": w,
                        "maxHeight": h,
                        "everyNthFrame": 1,
                    },
                )
            except Exception as e:
                logger.debug(f"Restart screencast warning after resize: {e}")

        await self.capture_frame()
        await self.broadcast_event({
            "type": "viewport_updated",
            "viewport": {"width": w, "height": h},
            "label": f"Viewport resized to {w}x{h}",
        })
        return {"session_id": self.session_id, "status": "resized", "viewport": {"width": w, "height": h}}

    async def highlight_element(self, selector: str) -> Dict[str, Any]:
        """Scroll element into view in Chromium and show brief visual telemetry box."""
        if not self.page:
            raise RuntimeError("Browser is not running.")
        locator = self.page.locator(selector).first
        try:
            await locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        box = await locator.bounding_box()
        if not box:
            return {"status": "not_found", "selector": selector}

        # Flash an ephemeral highlight ring directly in the headless viewport for crystal-clear feedback
        try:
            await self.page.evaluate(
                """(b) => {
                    const prev = document.getElementById('__cb_inspect_box');
                    if (prev) prev.remove();
                    const el = document.createElement('div');
                    el.id = '__cb_inspect_box';
                    Object.assign(el.style, {
                        position: 'fixed',
                        left: b.x + 'px',
                        top: b.y + 'px',
                        width: b.width + 'px',
                        height: b.height + 'px',
                        border: '2px solid #0071e3',
                        background: 'rgba(0, 113, 227, 0.12)',
                        borderRadius: '4px',
                        pointerEvents: 'none',
                        zIndex: '2147483647',
                        boxShadow: '0 0 0 3px rgba(0, 113, 227, 0.2)',
                        transition: 'opacity 0.25s ease'
                    });
                    document.documentElement.appendChild(el);
                    setTimeout(() => {
                        el.style.opacity = '0';
                        setTimeout(() => el.remove(), 260);
                    }, 1000);
                }""",
                box,
            )
        except Exception:
            pass

        await self.capture_frame()
        await self.broadcast_event({
            "type": "agent_action",
            "action": "highlight",
            "x": box["x"] + box["width"] / 2.0,
            "y": box["y"] + box["height"] / 2.0,
            "selector": selector,
            "label": f"Inspecting element: {selector}",
        })
        return {"status": "highlighted", "selector": selector, "box": box}

    async def click(
        self,
        selector: Optional[str] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
        button: str = "left",
    ) -> Dict[str, Any]:
        """Click by CSS selector, text locator, or exact (x, y) coordinates."""
        if not self.page:
            raise RuntimeError("Browser is not running.")

        click_x, click_y = x, y

        if selector:
            locator = self.page.locator(selector).first
            try:
                await locator.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            box = await locator.bounding_box()
            if box:
                click_x = box["x"] + (box["width"] / 2.0)
                click_y = box["y"] + (box["height"] / 2.0)
            else:
                # Fallback to direct locator click when bounding box is unavailable
                await self.broadcast_event({
                    "type": "agent_action",
                    "action": "click",
                    "selector": selector,
                    "label": f"Hermes clicking {selector}",
                })
                await locator.click(button=button, timeout=5000)
                await asyncio.sleep(0.3)
                title = await self.page.title()
                return {
                    "status": "clicked",
                    "x": None,
                    "y": None,
                    "selector": selector,
                    "current_url": self.page.url,
                    "title": title,
                }

        if click_x is None or click_y is None:
            raise ValueError("Either selector or (x, y) coordinates must be provided.")

        # Broadcast visual telemetry so user sees Hermes cursor clicking on the live canvas
        await self.broadcast_event({
            "type": "agent_action",
            "action": "click",
            "x": click_x,
            "y": click_y,
            "selector": selector or "",
            "label": f"Hermes clicked at ({int(click_x)}, {int(click_y)})",
        })

        await self.page.mouse.click(click_x, click_y, button=button)
        await asyncio.sleep(0.3)

        title = await self.page.title()
        return {
            "status": "clicked",
            "x": click_x,
            "y": click_y,
            "selector": selector,
            "current_url": self.page.url,
            "title": title,
        }

    async def type_text(
        self,
        text: str,
        selector: Optional[str] = None,
        clear_first: Optional[bool] = None,
        press_enter: bool = False,
    ) -> Dict[str, Any]:
        """Type text into an input field or active element."""
        if not self.page:
            raise RuntimeError("Browser is not running.")

        should_clear = bool(selector) if clear_first is None else bool(clear_first)

        if selector:
            locator = self.page.locator(selector).first
            try:
                await locator.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            box = await locator.bounding_box()
            if box:
                await self.broadcast_event({
                    "type": "agent_action",
                    "action": "focus",
                    "x": box["x"] + 10,
                    "y": box["y"] + (box["height"] / 2.0),
                    "selector": selector,
                    "label": f"Hermes focusing input: {selector}",
                })
            if should_clear:
                try:
                    await locator.fill("")
                except Exception:
                    await locator.focus()
                    await self.page.keyboard.press("Control+a")
                    await self.page.keyboard.press("Backspace")
            await locator.focus()

        await self.broadcast_event({
            "type": "agent_action",
            "action": "type",
            "text": text,
            "selector": selector or "",
            "label": f"Hermes typing into {selector or 'active element'}",
        })

        await self.page.keyboard.type(text, delay=35)

        if press_enter:
            await self.page.keyboard.press("Enter")
            await asyncio.sleep(0.4)

        return {
            "status": "typed",
            "text": text,
            "selector": selector,
            "pressed_enter": press_enter,
            "current_url": self.page.url,
        }

    async def scroll(self, direction: str = "down", amount: int = 400) -> Dict[str, Any]:
        """Scroll page up or down."""
        if not self.page:
            raise RuntimeError("Browser is not running.")

        await self.broadcast_event({
            "type": "agent_action",
            "action": "scroll",
            "direction": direction,
            "label": f"Hermes scrolling {direction}",
        })

        if direction == "top":
            await self.page.evaluate("window.scrollTo(0, 0)")
            return {"status": "scrolled", "direction": "top"}
        if direction == "bottom":
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            return {"status": "scrolled", "direction": "bottom"}

        delta_y = amount if direction == "down" else -amount
        await self.page.mouse.wheel(0, delta_y)
        await asyncio.sleep(0.2)
        return {"status": "scrolled", "direction": direction, "amount": amount}

    async def eval_js(self, script: str) -> Any:
        """Execute arbitrary JavaScript in the page context."""
        if not self.page:
            raise RuntimeError("Browser is not running.")
        result = await self.page.evaluate(script)
        return {"status": "evaluated", "result": result}

    async def get_state(self, include_dom_tree: bool = True) -> Dict[str, Any]:
        """Extract current page details, URL, title, and interactive DOM elements."""
        if not self.page:
            return {"status": "closed", "url": None, "title": None, "elements": []}

        title = await self.page.title()
        url = self.page.url

        elements: List[Dict[str, Any]] = []
        if include_dom_tree:
            js_extract = """
            () => {
                const results = [];
                const query = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="checkbox"]';
                const nodes = document.querySelectorAll(query);
                for (let i = 0; i < nodes.length && results.length < 60; i++) {
                    const el = nodes[i];
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                        continue;
                    }
                    const rect = el.getBoundingClientRect();
                    if (
                        rect.width > 0 &&
                        rect.height > 0 &&
                        rect.bottom > 0 &&
                        rect.top < window.innerHeight &&
                        rect.right > 0 &&
                        rect.left < window.innerWidth
                    ) {
                        const elType = (el.getAttribute('type') || el.type || '').toLowerCase().trim();
                        const acTokens = (el.getAttribute('autocomplete') || '').toLowerCase().trim().split(/\\s+/).filter(Boolean);
                        const isSensitive = (
                            elType === 'password'
                            || acTokens.includes('one-time-code')
                            || acTokens.includes('current-password')
                            || acTokens.includes('new-password')
                        );
                        let text = isSensitive
                            ? '[redacted]'
                            : (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim();
                        if (!isSensitive && text.length > 50) text = text.substring(0, 50) + '...';

                        let selector = '';
                        const tag = el.tagName.toLowerCase();
                        if (el.id) {
                            selector = '#' + CSS.escape(el.id);
                        } else if (el.name) {
                            selector = tag + '[name="' + el.name.replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"') + '"]';
                        } else if (el.className && typeof el.className === 'string') {
                            const firstClass = el.className.trim().split(/\\s+/)[0];
                            if (firstClass) selector = tag + '.' + CSS.escape(firstClass);
                        }
                        if (!selector) selector = tag;

                        results.push({
                            tag: tag,
                            type: el.type || null,
                            id: el.id || null,
                            name: el.name || null,
                            text: text,
                            selector: selector,
                            box: {
                                x: Math.round(rect.x),
                                y: Math.round(rect.y),
                                width: Math.round(rect.width),
                                height: Math.round(rect.height)
                            }
                        });
                    }
                }
                return results;
            }
            """
            try:
                elements = await self.page.evaluate(js_extract)
            except Exception as e:
                logger.warning(f"Failed to extract DOM elements: {e}")
                elements = []

        self.current_title = title
        self.current_url = url
        return {
            "status": "active",
            "session_id": self.session_id,
            "url": url,
            "title": title,
            "viewport": {"width": self.width, "height": self.height},
            "intervention_active": self.intervention_active,
            "user_takeover_active": self.user_takeover_active,
            "can_go_back": self._nav_index > 0,
            "can_go_forward": 0 <= self._nav_index < len(self._nav_history) - 1,
            "interactive_elements_count": len(elements),
            "elements": elements,
        }

    def get_console_logs(self) -> List[Dict[str, Any]]:
        return list(self.console_logs)

    def get_network_logs(self) -> List[Dict[str, Any]]:
        return list(self.network_logs)

    def get_action_history(self) -> List[Dict[str, Any]]:
        return list(self.action_history)

    async def get_session_snapshot(self) -> Dict[str, Any]:
        """Fast lightweight metadata snapshot for multi-window Studio cards."""
        url = self.page.url if self.page else self.current_url
        title = self.window_title or self.current_title
        if self.page and self.is_running:
            try:
                page_title = await self.page.title()
                if page_title:
                    title = page_title
                    self.current_title = page_title
            except Exception:
                pass
        return {
            "session_id": self.session_id,
            "title": title or "Untitled Session",
            "window_title": self.window_title,
            "url": url or "about:blank",
            "running": self.is_running,
            "viewport": {"width": self.width, "height": self.height},
            "intervention_active": self.intervention_active,
            "intervention_reason": self.intervention_reason,
            "intervention_guidance": self.intervention_guidance,
            "intervention_timeout": self.get_intervention_remaining_seconds(),
            "user_takeover_active": self.user_takeover_active,
            "can_go_back": self._nav_index > 0,
            "can_go_forward": 0 <= self._nav_index < len(self._nav_history) - 1,
            "console_count": len(self.console_logs),
            "network_count": len(self.network_logs),
            "action_count": len(self.action_history),
            "created_at": self.created_at,
        }

    def get_intervention_remaining_seconds(self) -> int:
        if not self.intervention_active or self.intervention_deadline is None:
            return 0
        return max(0, int(self.intervention_deadline - time.monotonic()))

    async def request_human_intervention(
        self,
        reason: str,
        guidance: str,
        timeout_seconds: int = 300,
    ) -> Dict[str, Any]:
        """Pause agent execution, trigger human handoff alert, and await user resolution."""
        logger.info(f"HUMAN INTERVENTION REQUESTED: {reason}")
        self.intervention_active = True
        self.intervention_reason = reason
        self.intervention_guidance = guidance
        self.intervention_deadline = time.monotonic() + float(timeout_seconds)
        self.intervention_event = asyncio.Event()

        # Broadcast critical alert to the Canvas Studio UI
        await self.broadcast_event({
            "type": "intervention_required",
            "reason": reason,
            "guidance": guidance,
            "timeout": timeout_seconds,
            "url": self.page.url if self.page else "",
            "title": await self.page.title() if self.page else "",
        })

        try:
            await asyncio.wait_for(self.intervention_event.wait(), timeout=float(timeout_seconds))
            status = "resolved"
            msg = "User successfully performed required action and resumed Hermes Agent."
        except asyncio.TimeoutError:
            status = "timeout"
            msg = f"Human intervention timed out after {timeout_seconds} seconds."
        finally:
            self.intervention_active = False
            self.intervention_reason = None
            self.intervention_guidance = None
            self.intervention_deadline = None

        new_state = await self.get_state(include_dom_tree=False)
        return {
            "status": status,
            "message": msg,
            "current_url": new_state.get("url"),
            "current_title": new_state.get("title"),
        }

    def resolve_human_intervention(self, user_note: str = ""):
        """Thread-safe resolver called when user clicks 'Resume Hermes' in the UI."""
        if not self.intervention_active and not self.intervention_event:
            return

        self.intervention_active = False
        event_payload = {
            "type": "intervention_resolved",
            "note": user_note,
            "label": "Human handoff resolved. Hermes resuming control.",
        }

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop and running_loop == self._loop:
            if self.intervention_event:
                self.intervention_event.set()
            running_loop.create_task(self.broadcast_event(event_payload))
        elif self._loop and self._loop.is_running():
            if self.intervention_event:
                self._loop.call_soon_threadsafe(self.intervention_event.set)
            asyncio.run_coroutine_threadsafe(self.broadcast_event(event_payload), self._loop)
        elif self.intervention_event:
            self.intervention_event.set()

    async def dispatch_user_input(self, event: Dict[str, Any]):
        """Relay real-time mouse and keyboard events from the user's canvas to Chromium."""
        if not self.page:
            return

        event_type = event.get("type")
        x = event.get("x")
        y = event.get("y")

        try:
            if event_type == "mouse_move" and x is not None and y is not None:
                await self.page.mouse.move(x, y)
            elif event_type == "mouse_down" and x is not None and y is not None:
                button = event.get("button", "left")
                await self.page.mouse.move(x, y)
                await self.page.mouse.down(button=button)
            elif event_type == "mouse_up" and x is not None and y is not None:
                button = event.get("button", "left")
                await self.page.mouse.up(button=button)
            elif event_type == "click" and x is not None and y is not None:
                button = event.get("button", "left")
                await self.page.mouse.click(x, y, button=button)
            elif event_type == "wheel":
                dx = event.get("deltaX", 0)
                dy = event.get("deltaY", 0)
                await self.page.mouse.wheel(dx, dy)
            elif event_type == "key_down":
                key = self._normalize_key(event.get("key"))
                if key:
                    await self.page.keyboard.down(key)
            elif event_type == "key_up":
                key = self._normalize_key(event.get("key"))
                if key:
                    await self.page.keyboard.up(key)
            elif event_type == "insert_text":
                text = event.get("text", "")
                if text:
                    await self.page.keyboard.insert_text(text)
        except Exception as e:
            logger.debug(f"Error dispatching user input: {e}")

    @staticmethod
    def _normalize_key(key: Optional[str]) -> Optional[str]:
        if not key or key in _UNSUPPORTED_PLAYWRIGHT_KEYS:
            return None
        if key == "Spacebar":
            return " "
        return key

    async def close(self):
        """Clean up CDP, page, and browser instances."""
        self.is_running = False
        if self.intervention_event and not self.intervention_event.is_set():
            self.intervention_event.set()

        if self.cdp:
            try:
                await self.cdp.send("Page.stopScreencast")
                await self.cdp.detach()
            except Exception:
                pass
            self.cdp = None

        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None

        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None

        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None

        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

        logger.info("BrowserEngine shutdown complete.")
