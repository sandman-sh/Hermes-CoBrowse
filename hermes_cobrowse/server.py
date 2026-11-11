"""FastAPI + WebSocket Streaming Server for Hermes CoBrowse.

Streams high-FPS CDP screencast frames for single or multiple concurrent browser
windows to the Hermes Desktop Spatial Canvas, relays bidirectional user input
and window commands, and coordinates human intervention handoffs.
"""

import asyncio
import json
import logging
import os
import socket
import threading
from typing import Any, Dict, Optional, Set
from fastapi import Body, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn

from hermes_cobrowse.engine import BrowserEngine

logger = logging.getLogger("cobrowse.server")


def find_free_port(start_port: int = 9876) -> int:
    """Find an available, bindable TCP port starting from start_port."""
    for port in range(start_port, start_port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    # Fallback: let OS choose an ephemeral port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _SingleEngineAdapter:
    """Adapter allowing CoBrowseServer to wrap a standalone BrowserEngine."""

    def __init__(self, engine: BrowserEngine):
        self.sessions: Dict[str, BrowserEngine] = {engine.session_id: engine}
        self.active_session_id: Optional[str] = engine.session_id

    @property
    def engine(self) -> Optional[BrowserEngine]:
        return self.sessions.get(self.active_session_id) if self.active_session_id else None

    def get_session(self, session_id: Optional[str] = None) -> Optional[BrowserEngine]:
        sid = session_id or self.active_session_id
        return self.sessions.get(sid) if sid else self.engine

    def focus_session(self, session_id: str) -> Optional[BrowserEngine]:
        if session_id in self.sessions:
            self.active_session_id = session_id
            return self.sessions[session_id]
        return None


class CoBrowseServer:
    """Encapsulates the FastAPI application, multi-window WebSocket hubs, and Studio REST API."""

    def __init__(self, manager_or_engine: Any, host: str = "127.0.0.1", port: int = 9876):
        if isinstance(manager_or_engine, BrowserEngine):
            self.manager = _SingleEngineAdapter(manager_or_engine)
        else:
            self.manager = manager_or_engine

        self.host = host
        self.port = port
        self.stream_websockets: Dict[WebSocket, Optional[str]] = {}
        self.studio_websockets: Set[WebSocket] = set()
        self._attached_listeners: Dict[str, tuple] = {}

        self.app = FastAPI(title="Hermes CoBrowse Spatial Canvas Gateway")
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._setup_routes()
        for eng in list(self.manager.sessions.values()):
            self.attach_session(eng)

    @property
    def engine(self) -> Optional[BrowserEngine]:
        return self.manager.engine

    @property
    def active_websockets(self) -> Set[WebSocket]:
        return set(self.stream_websockets.keys()) | self.studio_websockets

    def attach_session(self, engine: BrowserEngine):
        """Wire CDP screencast frames and telemetry events for a BrowserEngine session."""
        sid = engine.session_id
        if sid in self._attached_listeners:
            return

        async def on_frame(data_b64: str, metadata: Dict[str, Any]):
            if not self.stream_websockets and not self.studio_websockets:
                return
            studio_payload = json.dumps({
                "type": "frame",
                "session_id": sid,
                "data": data_b64,
                "metadata": metadata,
            })
            await self._broadcast_studio_raw(studio_payload)

            if self.stream_websockets:
                stream_payload = json.dumps({
                    "type": "frame",
                    "session_id": sid,
                    "data": data_b64,
                    "metadata": metadata,
                })
                await self._broadcast_stream_raw(stream_payload, sid)

        async def on_event(event: Dict[str, Any]):
            if not self.stream_websockets and not self.studio_websockets:
                return
            enriched = {"session_id": sid, **event}
            payload = json.dumps(enriched)
            await self._broadcast_studio_raw(payload)
            if self.stream_websockets:
                await self._broadcast_stream_raw(payload, sid)

        engine.add_frame_listener(on_frame)
        engine.add_event_listener(on_event)
        self._attached_listeners[sid] = (on_frame, on_event)

    def detach_session(self, engine: BrowserEngine):
        """Remove frame and event listeners when closing a session."""
        sid = engine.session_id
        pair = self._attached_listeners.pop(sid, None)
        if pair:
            on_frame, on_event = pair
            engine.remove_frame_listener(on_frame)
            engine.remove_event_listener(on_event)

    def _resolve_engine(self, session_id: Optional[str] = None) -> Optional[BrowserEngine]:
        return self.manager.get_session(session_id)

    def _setup_routes(self):
        static_dir = os.path.join(os.path.dirname(__file__), "web")
        if not os.path.exists(static_dir):
            os.makedirs(static_dir, exist_ok=True)

        self.app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @self.app.get("/")
        async def get_index():
            index_path = os.path.join(static_dir, "index.html")
            if os.path.exists(index_path):
                return FileResponse(index_path, headers={"Cache-Control": "no-store"})
            eng = self._resolve_engine()
            return JSONResponse({"status": "running", "engine": bool(eng and eng.is_running)})

        @self.app.get("/health")
        async def get_health():
            eng = self._resolve_engine()
            return {
                "status": "healthy",
                "running": bool(eng and eng.is_running),
                "url": eng.page.url if eng and eng.page else None,
                "active_session_id": self.manager.active_session_id,
                "session_count": len(self.manager.sessions),
                "clients_connected": len(self.stream_websockets) + len(self.studio_websockets),
                "intervention_active": bool(eng and eng.intervention_active),
            }

        @self.app.get("/api/state")
        async def api_get_state(session_id: Optional[str] = Query(default=None)):
            eng = self._resolve_engine(session_id)
            if not eng:
                return JSONResponse({"error": "No active browser session"}, status_code=404)
            return await eng.get_state()

        @self.app.post("/api/resume")
        async def api_resume(payload: Optional[Dict[str, Any]] = Body(default=None)):
            body = payload or {}
            eng = self._resolve_engine(body.get("session_id"))
            if not eng:
                return JSONResponse({"error": "No active browser session"}, status_code=404)
            note = body.get("note", "Manual resume from UI")
            eng.resolve_human_intervention(note)
            return {"status": "ok", "session_id": eng.session_id, "message": "Intervention resolved"}

        @self.app.post("/api/navigate")
        async def api_navigate(payload: Dict[str, Any] = Body(...)):
            url = payload.get("url")
            if not url:
                return JSONResponse({"error": "Missing url"}, status_code=400)
            eng = self._resolve_engine(payload.get("session_id"))
            if not eng:
                return JSONResponse({"error": "No active browser session"}, status_code=404)
            try:
                return await eng.navigate(url)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ---- Multi-Session Spatial Canvas Studio API ----

        @self.app.get("/api/sessions")
        async def api_list_sessions():
            snapshots = []
            for eng in list(self.manager.sessions.values()):
                snapshots.append(await eng.get_session_snapshot())
            return {
                "active_session_id": self.manager.active_session_id,
                "sessions": snapshots,
            }

        @self.app.post("/api/sessions")
        async def api_create_session(payload: Optional[Dict[str, Any]] = Body(default=None)):
            if not hasattr(self.manager, "create_session_async"):
                return JSONResponse({"error": "Multi-session creation unsupported"}, status_code=400)
            body = payload or {}
            url = body.get("url") or "about:blank"
            width = int(body.get("width", 1280))
            height = int(body.get("height", 800))
            title = str(body.get("title") or "")
            session_id = body.get("session_id")
            try:
                res = await self.manager.create_session_async(
                    initial_url=url,
                    width=width,
                    height=height,
                    session_id=session_id,
                    title=title,
                    force_new=True,
                )
                return res
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.delete("/api/sessions/{session_id}")
        async def api_close_session(session_id: str):
            if not hasattr(self.manager, "close_session_async"):
                return JSONResponse({"error": "Multi-session close unsupported"}, status_code=400)
            if session_id not in self.manager.sessions:
                return JSONResponse({"error": f"Unknown session: {session_id}"}, status_code=404)
            res = await self.manager.close_session_async(session_id)
            return res

        @self.app.post("/api/sessions/{session_id}/focus")
        async def api_focus_session(session_id: str):
            eng = self.manager.focus_session(session_id) if hasattr(self.manager, "focus_session") else None
            if not eng:
                return JSONResponse({"error": f"Unknown session: {session_id}"}, status_code=404)
            await self.broadcast_json({"type": "session_focused", "session_id": session_id})
            return {"status": "focused", "session_id": session_id}

        @self.app.post("/api/sessions/{session_id}/navigate")
        async def api_session_navigate(session_id: str, payload: Dict[str, Any] = Body(...)):
            eng = self._resolve_engine(session_id)
            if not eng:
                return JSONResponse({"error": f"Unknown session: {session_id}"}, status_code=404)
            url = payload.get("url")
            if not url:
                return JSONResponse({"error": "Missing url"}, status_code=400)
            try:
                return await eng.navigate(url)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.post("/api/sessions/{session_id}/back")
        async def api_session_back(session_id: str):
            eng = self._resolve_engine(session_id)
            if not eng:
                return JSONResponse({"error": f"Unknown session: {session_id}"}, status_code=404)
            try:
                return await eng.go_back()
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.post("/api/sessions/{session_id}/forward")
        async def api_session_forward(session_id: str):
            eng = self._resolve_engine(session_id)
            if not eng:
                return JSONResponse({"error": f"Unknown session: {session_id}"}, status_code=404)
            try:
                return await eng.go_forward()
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.post("/api/sessions/{session_id}/reload")
        async def api_session_reload(session_id: str):
            eng = self._resolve_engine(session_id)
            if not eng:
                return JSONResponse({"error": f"Unknown session: {session_id}"}, status_code=404)
            try:
                return await eng.reload()
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.post("/api/sessions/{session_id}/resize")
        async def api_session_resize(session_id: str, payload: Dict[str, Any] = Body(...)):
            eng = self._resolve_engine(session_id)
            if not eng:
                return JSONResponse({"error": f"Unknown session: {session_id}"}, status_code=404)
            try:
                w = int(payload.get("width", eng.width))
                h = int(payload.get("height", eng.height))
                return await eng.resize_viewport(w, h)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.post("/api/sessions/{session_id}/resume")
        async def api_session_resume(session_id: str, payload: Optional[Dict[str, Any]] = Body(default=None)):
            eng = self._resolve_engine(session_id)
            if not eng:
                return JSONResponse({"error": f"Unknown session: {session_id}"}, status_code=404)
            note = (payload or {}).get("note", "Manual resume from UI")
            eng.resolve_human_intervention(note)
            return {"status": "ok", "session_id": session_id}

        @self.app.post("/api/sessions/{session_id}/highlight")
        async def api_session_highlight(session_id: str, payload: Dict[str, Any] = Body(...)):
            eng = self._resolve_engine(session_id)
            if not eng:
                return JSONResponse({"error": f"Unknown session: {session_id}"}, status_code=404)
            selector = payload.get("selector")
            if not selector:
                return JSONResponse({"error": "Missing selector"}, status_code=400)
            try:
                return await eng.highlight_element(selector)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.get("/api/sessions/{session_id}/state")
        async def api_session_state(session_id: str):
            eng = self._resolve_engine(session_id)
            if not eng:
                return JSONResponse({"error": f"Unknown session: {session_id}"}, status_code=404)
            return await eng.get_state(include_dom_tree=True)

        @self.app.get("/api/sessions/{session_id}/telemetry")
        async def api_session_telemetry(session_id: str):
            eng = self._resolve_engine(session_id)
            if not eng:
                return JSONResponse({"error": f"Unknown session: {session_id}"}, status_code=404)
            return {
                "session_id": session_id,
                "console_logs": eng.get_console_logs(),
                "network_logs": eng.get_network_logs(),
                "action_history": eng.get_action_history(),
            }

        @self.app.get("/api/sessions/{session_id}/screenshot")
        async def api_session_screenshot(session_id: str):
            eng = self._resolve_engine(session_id)
            if not eng:
                return JSONResponse({"error": f"Unknown session: {session_id}"}, status_code=404)
            try:
                png_bytes = await eng.capture_screenshot_bytes("png")
                return Response(
                    content=png_bytes,
                    media_type="image/png",
                    headers={
                        "Content-Disposition": f'attachment; filename="cobrowse-{session_id}.png"',
                        "Cache-Control": "no-store",
                    },
                )
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ---- Single-Session Backwards-Compatible WebSocket (/ws/stream) ----

        @self.app.websocket("/ws/stream")
        async def websocket_stream_endpoint(websocket: WebSocket, session_id: Optional[str] = Query(default=None)):
            await websocket.accept()

            try:
                eng = self._resolve_engine(session_id)
                init_msg = {
                    "type": "init",
                    "session_id": eng.session_id if eng else None,
                    "running": bool(eng and eng.is_running),
                    "intervention_active": bool(eng and eng.intervention_active),
                    "intervention_reason": eng.intervention_reason if eng else None,
                    "intervention_guidance": eng.intervention_guidance if eng else None,
                    "intervention_timeout": eng.get_intervention_remaining_seconds() if eng else 0,
                    "viewport": {"width": eng.width, "height": eng.height} if eng else {"width": 1280, "height": 800},
                    "url": eng.page.url if eng and eng.page else "about:blank",
                }
                await websocket.send_text(json.dumps(init_msg))
                self.stream_websockets[websocket] = session_id
                logger.info(f"Stream client connected (session={session_id or 'active'}).")

                if eng:
                    if not eng.latest_frame_b64 and eng.is_running:
                        await eng.capture_frame()
                    if eng.latest_frame_b64:
                        await websocket.send_text(json.dumps({
                            "type": "frame",
                            "session_id": eng.session_id,
                            "data": eng.latest_frame_b64,
                            "metadata": eng.latest_metadata,
                        }))

                while True:
                    data_str = await websocket.receive_text()
                    try:
                        msg = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    target_sid = msg.get("session_id") or session_id
                    target_eng = self._resolve_engine(target_sid)
                    if not target_eng:
                        continue

                    msg_type = msg.get("type")
                    if msg_type == "resume_agent":
                        target_eng.resolve_human_intervention(msg.get("note", ""))
                    elif msg_type == "takeover_toggle":
                        target_eng.user_takeover_active = bool(msg.get("enabled", False))
                        await self.broadcast_json({
                            "type": "takeover_status",
                            "session_id": target_eng.session_id,
                            "enabled": target_eng.user_takeover_active,
                        })
                    else:
                        await target_eng.dispatch_user_input(msg)

            except WebSocketDisconnect:
                self.stream_websockets.pop(websocket, None)
            except Exception as e:
                logger.debug(f"Stream WebSocket error: {e}")
                self.stream_websockets.pop(websocket, None)

        # ---- Multi-Window Spatial Canvas Studio WebSocket (/ws/studio) ----

        @self.app.websocket("/ws/studio")
        async def websocket_studio_endpoint(websocket: WebSocket):
            await websocket.accept()

            try:
                snapshots = []
                for eng in list(self.manager.sessions.values()):
                    snapshots.append(await eng.get_session_snapshot())

                await websocket.send_text(json.dumps({
                    "type": "studio_init",
                    "active_session_id": self.manager.active_session_id,
                    "sessions": snapshots,
                }))
                self.studio_websockets.add(websocket)
                logger.info(f"Studio client connected. Active studio clients: {len(self.studio_websockets)}")

                # Push initial frame for every running window immediately
                for eng in list(self.manager.sessions.values()):
                    if not eng.latest_frame_b64 and eng.is_running:
                        await eng.capture_frame()
                    if eng.latest_frame_b64:
                        await websocket.send_text(json.dumps({
                            "type": "frame",
                            "session_id": eng.session_id,
                            "data": eng.latest_frame_b64,
                            "metadata": eng.latest_metadata,
                        }))

                while True:
                    data_str = await websocket.receive_text()
                    try:
                        msg = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type")
                    target_sid = msg.get("session_id") or self.manager.active_session_id

                    if msg_type == "focus_session" and target_sid:
                        if hasattr(self.manager, "focus_session") and self.manager.focus_session(target_sid):
                            await self.broadcast_json({"type": "session_focused", "session_id": target_sid})
                        continue

                    target_eng = self._resolve_engine(target_sid)
                    if not target_eng:
                        continue

                    if msg_type == "resume_agent":
                        target_eng.resolve_human_intervention(msg.get("note", ""))
                    elif msg_type == "takeover_toggle":
                        target_eng.user_takeover_active = bool(msg.get("enabled", False))
                        await self.broadcast_json({
                            "type": "takeover_status",
                            "session_id": target_eng.session_id,
                            "enabled": target_eng.user_takeover_active,
                        })
                    elif msg_type == "request_frame":
                        await target_eng.capture_frame()
                    else:
                        await target_eng.dispatch_user_input(msg)

            except WebSocketDisconnect:
                self.studio_websockets.discard(websocket)
            except Exception as e:
                logger.debug(f"Studio WebSocket error: {e}")
                self.studio_websockets.discard(websocket)

    async def _safe_send(self, ws: WebSocket, payload: str) -> bool:
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=2.0)
            return True
        except Exception:
            return False

    async def _broadcast_stream_raw(self, payload: str, session_id: str):
        dead_sockets = []
        active_sid = self.manager.active_session_id
        for ws, bound_sid in list(self.stream_websockets.items()):
            if bound_sid is not None and bound_sid != session_id:
                continue
            if bound_sid is None and active_sid and active_sid != session_id:
                continue
            if not await self._safe_send(ws, payload):
                dead_sockets.append(ws)
        for dead in dead_sockets:
            self.stream_websockets.pop(dead, None)

    async def _broadcast_studio_raw(self, payload: str):
        dead_sockets = []
        for ws in list(self.studio_websockets):
            if not await self._safe_send(ws, payload):
                dead_sockets.append(ws)
        for dead in dead_sockets:
            self.studio_websockets.discard(dead)

    async def broadcast_json(self, data: Dict[str, Any]):
        payload = json.dumps(data)
        await self._broadcast_studio_raw(payload)
        sid = data.get("session_id")
        if sid:
            await self._broadcast_stream_raw(payload, sid)
        else:
            dead_sockets = []
            for ws in list(self.stream_websockets.keys()):
                if not await self._safe_send(ws, payload):
                    dead_sockets.append(ws)
            for dead in dead_sockets:
                self.stream_websockets.pop(dead, None)

    def get_preview_url(self) -> str:
        return f"http://{self.host}:{self.port}"
