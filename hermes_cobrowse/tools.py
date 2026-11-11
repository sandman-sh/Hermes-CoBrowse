"""Hermes Agent Tool Handlers for CoBrowse.

Implements all tool endpoints callable by Hermes Agent and manages single or
multiple concurrent BrowserEngine windows attached to the Spatial Canvas gateway:
cobrowse_open, cobrowse_navigate, cobrowse_click, cobrowse_type,
cobrowse_request_handoff, cobrowse_get_state, cobrowse_scroll,
cobrowse_eval_js, and cobrowse_close.
"""

import asyncio
import json
import logging
import threading
from typing import Any, Dict, List, Optional
import uvicorn

from hermes_cobrowse.engine import BrowserEngine
from hermes_cobrowse.server import CoBrowseServer, find_free_port

logger = logging.getLogger("cobrowse.tools")


class CoBrowseManager:
    """Singleton managing concurrent browser sessions, streaming server, and background async loop."""

    _instance: Optional["CoBrowseManager"] = None

    def __init__(self):
        self.sessions: Dict[str, BrowserEngine] = {}
        self.active_session_id: Optional[str] = None
        self._session_counter: int = 0

        self.server: Optional[CoBrowseServer] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.uv_server: Optional[uvicorn.Server] = None
        self._uv_task: Optional[asyncio.Task] = None
        self.port: int = 9876
        self._ready_event = threading.Event()
        self._lock = threading.Lock()
        self._async_lock: Optional[asyncio.Lock] = None
        self.preview_url: str = ""

    @classmethod
    def get_instance(cls) -> "CoBrowseManager":
        if cls._instance is None:
            cls._instance = CoBrowseManager()
        return cls._instance

    @property
    def engine(self) -> Optional[BrowserEngine]:
        """Return the currently focused BrowserEngine (backwards-compatible single-engine accessor)."""
        if self.active_session_id and self.active_session_id in self.sessions:
            return self.sessions[self.active_session_id]
        for eng in self.sessions.values():
            return eng
        return None

    def get_session(self, session_id: Optional[str] = None) -> Optional[BrowserEngine]:
        if session_id:
            return self.sessions.get(session_id)
        return self.engine

    def focus_session(self, session_id: str) -> Optional[BrowserEngine]:
        eng = self.sessions.get(session_id)
        if eng:
            self.active_session_id = session_id
        return eng

    def _next_session_id(self) -> str:
        while True:
            self._session_counter += 1
            candidate = f"win-{self._session_counter}"
            if candidate not in self.sessions:
                return candidate

    def _get_async_lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def _ensure_loop_and_server_locked(self):
        """Must be called with self._lock held from an external thread."""
        if not self.thread or not self.thread.is_alive():
            self._ready_event.clear()
            self._async_lock = None
            self.thread = threading.Thread(
                target=self._run_loop, daemon=True, name="CoBrowseWorker"
            )
            self.thread.start()
            if not self._ready_event.wait(timeout=10):
                raise RuntimeError("Timed out starting CoBrowse background loop.")

        if self.uv_server is not None and self.server is not None:
            return

        async def _boot_server():
            self.port = find_free_port(9876)
            self.server = CoBrowseServer(self, host="127.0.0.1", port=self.port)
            self.preview_url = self.server.get_preview_url()

            config = uvicorn.Config(
                app=self.server.app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
            )
            self.uv_server = uvicorn.Server(config)
            self._uv_task = self.loop.create_task(self.uv_server.serve())

            for _ in range(100):
                if self.uv_server.started:
                    break
                if self._uv_task.done():
                    exc = self._uv_task.exception()
                    if exc:
                        raise exc
                    raise RuntimeError("CoBrowse Uvicorn server exited unexpectedly.")
                await asyncio.sleep(0.05)

        self.run_async(_boot_server(), timeout=20.0)

    async def create_session_async(
        self,
        initial_url: str = "about:blank",
        width: int = 1280,
        height: int = 800,
        session_id: Optional[str] = None,
        title: str = "",
        force_new: bool = False,
    ) -> Dict[str, Any]:
        """Create or reuse a browser session on the manager's event loop."""
        async with self._get_async_lock():
            sid = str(session_id).strip() if session_id else ""
            if not sid:
                if force_new or not self.sessions:
                    sid = self._next_session_id()
                else:
                    sid = self.active_session_id or next(iter(self.sessions))

            existing = self.sessions.get(sid)
            if existing and existing.is_running:
                self.active_session_id = sid
                if initial_url and initial_url != "about:blank":
                    nav_res = await existing.navigate(initial_url)
                    res = dict(nav_res)
                else:
                    res = {"status": "already_running", "url": existing.page.url if existing.page else "about:blank"}
                snapshot = await existing.get_session_snapshot()
                if self.server:
                    await self.server.broadcast_json({"type": "session_focused", "session_id": sid})
                return {
                    **res,
                    "session_id": sid,
                    "preview_url": self.preview_url,
                    "session": snapshot,
                    "total_sessions": len(self.sessions),
                }

            eng = BrowserEngine(width=width, height=height, session_id=sid, title=title)
            self.sessions[sid] = eng
            self.active_session_id = sid
            if self.server:
                self.server.attach_session(eng)

            try:
                start_res = await eng.start(initial_url=initial_url)
            except Exception:
                self.sessions.pop(sid, None)
                if self.server:
                    self.server.detach_session(eng)
                if self.active_session_id == sid:
                    self.active_session_id = next(iter(self.sessions), None)
                raise

            snapshot = await eng.get_session_snapshot()
            if self.server:
                await self.server.broadcast_json({
                    "type": "session_created",
                    "session_id": sid,
                    "active_session_id": self.active_session_id,
                    "session": snapshot,
                })

            return {
                **start_res,
                "session_id": sid,
                "preview_url": self.preview_url,
                "session": snapshot,
                "total_sessions": len(self.sessions),
            }

    async def close_session_async(self, session_id: str) -> Dict[str, Any]:
        """Close a specific browser window cleanly without stopping sibling sessions."""
        async with self._get_async_lock():
            eng = self.sessions.pop(session_id, None)
            if not eng:
                return {"status": "not_found", "session_id": session_id}

            if self.server:
                self.server.detach_session(eng)
            try:
                await eng.close()
            except Exception as e:
                logger.debug(f"Error closing session {session_id}: {e}")

            if self.active_session_id == session_id:
                self.active_session_id = next(iter(self.sessions), None)

            if self.server:
                await self.server.broadcast_json({
                    "type": "session_closed",
                    "session_id": session_id,
                    "active_session_id": self.active_session_id,
                })

            return {
                "status": "closed",
                "session_id": session_id,
                "active_session_id": self.active_session_id,
                "remaining_sessions": list(self.sessions.keys()),
            }

    def ensure_started(
        self,
        initial_url: str = "about:blank",
        width: int = 1280,
        height: int = 800,
        session_id: Optional[str] = None,
        new_window: bool = False,
        title: str = "",
    ) -> Dict[str, Any]:
        """Ensure background loop, streaming server, and target browser window are running."""
        with self._lock:
            self._ensure_loop_and_server_locked()

        return self.run_async(
            self.create_session_async(
                initial_url=initial_url,
                width=width,
                height=height,
                session_id=session_id,
                title=title,
                force_new=bool(new_window),
            ),
            timeout=45.0,
        )

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready_event.set()
        self.loop.run_forever()

    def run_async(self, coro, timeout: float = 60.0) -> Any:
        """Run a coroutine safely on the background thread loop and wait for result."""
        if not self.loop or not self.loop.is_running():
            raise RuntimeError("CoBrowse background loop is not active.")
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def list_sessions_sync(self) -> List[Dict[str, Any]]:
        if not self.loop or not self.loop.is_running():
            return []

        async def _collect():
            return [await eng.get_session_snapshot() for eng in list(self.sessions.values())]

        return self.run_async(_collect(), timeout=10.0)

    def shutdown(self) -> Dict[str, Any]:
        """Shut down all browser engines and the background streaming server cleanly."""
        with self._lock:
            async def _shutdown_async():
                for sid, eng in list(self.sessions.items()):
                    if self.server:
                        self.server.detach_session(eng)
                    try:
                        await eng.close()
                    except Exception as e:
                        logger.debug(f"Error closing session {sid}: {e}")
                self.sessions.clear()
                self.active_session_id = None

                if self.uv_server:
                    self.uv_server.should_exit = True
                    if self._uv_task and not self._uv_task.done():
                        try:
                            await asyncio.wait_for(self._uv_task, timeout=3.0)
                        except Exception:
                            try:
                                await self.uv_server.shutdown()
                            except Exception:
                                pass

            if self.loop and self.loop.is_running():
                try:
                    self.run_async(_shutdown_async(), timeout=15.0)
                except Exception as e:
                    logger.debug(f"Error during shutdown: {e}")

            self.sessions.clear()
            self.active_session_id = None
            self.server = None
            self.uv_server = None
            self._uv_task = None
            self.preview_url = ""
            return {"status": "closed"}


def _extract_params(params: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """Support both dict positional invocation handler(params) and kwargs invocation."""
    if isinstance(params, dict):
        return params
    return kwargs


def _get_target_engine(mgr: CoBrowseManager, session_id: Optional[str] = None) -> BrowserEngine:
    if session_id:
        eng = mgr.get_session(session_id)
        if not eng:
            raise ValueError(f"Browser session '{session_id}' not found. Open it first with cobrowse_open.")
        mgr.focus_session(session_id)
        return eng
    mgr.ensure_started()
    eng = mgr.engine
    if not eng:
        raise RuntimeError("No active CoBrowse browser engine available.")
    return eng


# Hermes Tool Handlers
def handle_open(params: Optional[Dict[str, Any]] = None, **kwargs) -> str:
    try:
        args = _extract_params(params, **kwargs)
        url = args.get("url", "https://google.com")
        width = int(args.get("viewport_width", 1280))
        height = int(args.get("viewport_height", 800))
        session_id = args.get("session_id")
        new_window = bool(args.get("new_window", False))
        title = str(args.get("title") or "")

        mgr = CoBrowseManager.get_instance()
        res = mgr.ensure_started(
            initial_url=url,
            width=width,
            height=height,
            session_id=session_id,
            new_window=new_window,
            title=title,
        )
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def handle_navigate(params: Optional[Dict[str, Any]] = None, **kwargs) -> str:
    try:
        args = _extract_params(params, **kwargs)
        url = args.get("url")
        if not url:
            return json.dumps({"status": "error", "error": "Missing required parameter 'url'"})

        mgr = CoBrowseManager.get_instance()
        eng = _get_target_engine(mgr, args.get("session_id"))
        res = mgr.run_async(eng.navigate(url))
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def handle_get_state(params: Optional[Dict[str, Any]] = None, **kwargs) -> str:
    try:
        args = _extract_params(params, **kwargs)
        include_dom = bool(args.get("include_dom_tree", True))

        mgr = CoBrowseManager.get_instance()
        eng = _get_target_engine(mgr, args.get("session_id"))
        res = mgr.run_async(eng.get_state(include_dom_tree=include_dom))
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def handle_click(params: Optional[Dict[str, Any]] = None, **kwargs) -> str:
    try:
        args = _extract_params(params, **kwargs)
        selector = args.get("selector")
        x = args.get("x")
        y = args.get("y")
        button = args.get("button", "left")

        mgr = CoBrowseManager.get_instance()
        eng = _get_target_engine(mgr, args.get("session_id"))
        res = mgr.run_async(eng.click(selector=selector, x=x, y=y, button=button))
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def handle_type(params: Optional[Dict[str, Any]] = None, **kwargs) -> str:
    try:
        args = _extract_params(params, **kwargs)
        text = args.get("text", "")
        selector = args.get("selector")
        raw_clear = args.get("clear_first")
        clear_first = bool(selector) if raw_clear is None else bool(raw_clear)
        press_enter = bool(args.get("press_enter", False))

        mgr = CoBrowseManager.get_instance()
        eng = _get_target_engine(mgr, args.get("session_id"))
        res = mgr.run_async(
            eng.type_text(
                text=text,
                selector=selector,
                clear_first=clear_first,
                press_enter=press_enter,
            )
        )
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _is_unattended_env() -> bool:
    import os
    return any(
        str(os.environ.get(k, "")).strip().lower() in ("1", "true", "yes")
        for k in ("HERMES_UNATTENDED", "HERMES_CRON", "HERMES_NONINTERACTIVE")
    )


def handle_request_handoff(params: Optional[Dict[str, Any]] = None, **kwargs) -> str:
    try:
        args = _extract_params(params, **kwargs)
        reason = args.get("reason", "Human verification required")
        guidance = args.get("guidance", "Please resolve the verification on the screen.")
        raw_timeout = int(args.get("timeout_seconds", 300))
        timeout_sec = max(1, min(raw_timeout, 900))

        mgr = CoBrowseManager.get_instance()
        eng = _get_target_engine(mgr, args.get("session_id"))

        # Respect unattended/cron runs: avoid waiting up to timeout when no interactive viewer is connected.
        has_viewer = bool(
            mgr.server
            and (mgr.server.studio_websockets or mgr.server.stream_websockets)
        )
        if _is_unattended_env() and not has_viewer:
            return json.dumps({
                "status": "unattended_abort",
                "message": "Human handoff skipped cleanly in unattended mode without an active Spatial Canvas viewer.",
                "reason": reason,
            })

        res = mgr.run_async(
            eng.request_human_intervention(
                reason=reason,
                guidance=guidance,
                timeout_seconds=timeout_sec,
            ),
            timeout=float(timeout_sec) + 10.0,
        )
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def handle_scroll(params: Optional[Dict[str, Any]] = None, **kwargs) -> str:
    try:
        args = _extract_params(params, **kwargs)
        direction = args.get("direction", "down")
        amount = int(args.get("amount", 400))

        mgr = CoBrowseManager.get_instance()
        eng = _get_target_engine(mgr, args.get("session_id"))
        res = mgr.run_async(eng.scroll(direction=direction, amount=amount))
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def handle_eval_js(params: Optional[Dict[str, Any]] = None, **kwargs) -> str:
    try:
        args = _extract_params(params, **kwargs)
        script = args.get("script", "")

        mgr = CoBrowseManager.get_instance()
        eng = _get_target_engine(mgr, args.get("session_id"))
        res = mgr.run_async(eng.eval_js(script))
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def handle_close(params: Optional[Dict[str, Any]] = None, **kwargs) -> str:
    try:
        args = _extract_params(params, **kwargs)
        session_id = args.get("session_id")
        mgr = CoBrowseManager.get_instance()
        if session_id:
            if not mgr.loop or not mgr.loop.is_running():
                return json.dumps({"status": "not_running", "session_id": session_id})
            res = mgr.run_async(mgr.close_session_async(str(session_id)), timeout=15.0)
            return json.dumps(res)
        res = mgr.shutdown()
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})
