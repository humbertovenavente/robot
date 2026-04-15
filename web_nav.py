"""Web interface for QR-guided robot navigation.

Starts a QRNavigator in a background thread and exposes:

  GET  /           → HTML dashboard (live stream + status)
  GET  /stream     → MJPEG camera feed with QR overlay
  GET  /api/status → JSON snapshot of NavigatorState
  WS   /ws/status  → WebSocket push on every state change

Run:
    python web_nav.py [--config station_config.yaml] [--port 8080]

Then open http://localhost:8080 in a browser.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
import time
from typing import Set

import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from config import load_config
from navigator import QRNavigator, NavigatorState, build_navigator

log = logging.getLogger(__name__)

app = FastAPI(title="Bug Navigator")

# ── shared state ─────────────────────────────────────────────────────────────
_navigator: QRNavigator | None = None
_nav_thread: threading.Thread | None = None
_ws_clients: Set[WebSocket] = set()
_ws_lock = asyncio.Lock()

# ── WebSocket broadcast (called from navigator thread via asyncio) ─────────────
_loop: asyncio.AbstractEventLoop | None = None


def _nxt_connected() -> bool:
    """True if the NXT drive has an active USB brick connection."""
    if _navigator is None:
        return False
    drive = getattr(_navigator, "_drive", None)
    if drive is None:
        return False
    return getattr(drive, "_brick", None) is not None


def _on_state_change(state: NavigatorState) -> None:
    """Called by QRNavigator on every state transition; pushes update to all WS clients."""
    global _loop
    if _loop is None:
        return
    payload = json.dumps({
        "status":        state.status,
        "qr_payload":    state.qr_payload,
        "qr_cx":         state.qr_cx,
        "qr_area_pct":   round(state.qr_area_pct * 100, 1) if state.qr_area_pct else None,
        "frame_w":       state.frame_w,
        "nxt_connected": _nxt_connected(),
    })
    asyncio.run_coroutine_threadsafe(_broadcast(payload), _loop)


async def _broadcast(message: str) -> None:
    async with _ws_lock:
        dead = set()
        for ws in _ws_clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        _ws_clients.difference_update(dead)


# ── HTML dashboard ────────────────────────────────────────────────────────────
_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bug Navigator</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0f0f0f; color: #e8e8e8; font-family: 'Segoe UI', sans-serif; }
    header {
      padding: 14px 20px;
      background: #1a1a2e;
      display: flex; align-items: center; gap: 16px;
      border-bottom: 2px solid #7c3aed;
    }
    header h1 { font-size: 1.3rem; color: #a78bfa; }
    .badge {
      padding: 4px 12px; border-radius: 20px; font-size: 0.8rem;
      font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
    }
    .badge.idle        { background:#2d2d2d; color:#888; }
    .badge.searching   { background:#0e3a4a; color:#38bdf8; }
    .badge.centering   { background:#3a2600; color:#fb923c; }
    .badge.approaching { background:#0a2e0a; color:#4ade80; }
    .badge.arrived     { background:#2e0a2e; color:#e879f9; }
    main {
      display: grid;
      grid-template-columns: 1fr 300px;
      gap: 16px; padding: 16px;
      max-width: 1100px; margin: 0 auto;
    }
    #stream-box {
      background: #111; border-radius: 10px; overflow: hidden;
      aspect-ratio: 4/3; position: relative;
    }
    #stream-box img { width: 100%; height: 100%; object-fit: contain; }
    .panel {
      background: #1a1a2e; border-radius: 10px; padding: 16px;
      display: flex; flex-direction: column; gap: 14px;
    }
    .panel h2 { font-size: 0.9rem; color: #7c3aed; text-transform: uppercase; letter-spacing: .08em; }
    .stat-row { display: flex; justify-content: space-between; font-size: 0.85rem; }
    .stat-label { color: #888; }
    .stat-value { color: #e8e8e8; font-family: monospace; }
    .qr-value { color: #4ade80; font-size: 1.1rem; font-weight: 700; }
    .progress-bar-bg {
      background: #2a2a2a; border-radius: 6px; height: 10px; overflow: hidden;
    }
    .progress-bar-fill {
      height: 100%; background: #7c3aed; border-radius: 6px;
      transition: width 0.2s;
    }
    .btn-row { display: flex; gap: 10px; margin-top: 6px; }
    button {
      flex: 1; padding: 10px; border: none; border-radius: 8px;
      font-size: 0.85rem; font-weight: 700; cursor: pointer; transition: opacity 0.15s;
    }
    button:hover { opacity: 0.85; }
    #btn-start { background: #7c3aed; color: #fff; }
    #btn-stop  { background: #dc2626; color: #fff; }
    .dot {
      width: 10px; height: 10px; border-radius: 50%; background: #888;
      display: inline-block; margin-right: 6px;
    }
    .dot.on  { background: #4ade80; box-shadow: 0 0 6px #4ade80; }
    .dot.err { background: #ef4444; box-shadow: 0 0 6px #ef4444; }
    .nxt-badge {
      display: flex; align-items: center; gap: 6px;
      padding: 4px 12px; border-radius: 20px; font-size: 0.78rem; font-weight: 700;
      border: 1px solid #333;
    }
    .nxt-badge.connected    { background:#0a2e0a; color:#4ade80; border-color:#166534; }
    .nxt-badge.disconnected { background:#2d0a0a; color:#ef4444; border-color:#7f1d1d; }
    footer { text-align:center; padding: 12px; color: #444; font-size: 0.75rem; }
  </style>
</head>
<body>
<header>
  <h1>Bug Navigator</h1>
  <span id="status-badge" class="badge idle">idle</span>
  <span id="nxt-badge" class="nxt-badge disconnected">
    <span id="nxt-dot" class="dot err"></span>NXT desconectado
  </span>
  <span style="margin-left:auto; font-size:0.8rem; color:#555">
    <span id="ws-dot" class="dot"></span>live
  </span>
</header>

<main>
  <div id="stream-box">
    <img id="feed" src="/stream" alt="camera feed" />
  </div>

  <div class="panel">
    <h2>Navigation State</h2>

    <div class="stat-row">
      <span class="stat-label">Status</span>
      <span id="st-status" class="stat-value">—</span>
    </div>

    <div>
      <div class="stat-row" style="margin-bottom:4px">
        <span class="stat-label">QR detected</span>
        <span id="st-qr" class="qr-value">—</span>
      </div>
    </div>

    <div>
      <div class="stat-row" style="margin-bottom:6px">
        <span class="stat-label">Proximity</span>
        <span id="st-area" class="stat-value">0 %</span>
      </div>
      <div class="progress-bar-bg">
        <div id="prox-bar" class="progress-bar-fill" style="width:0%"></div>
      </div>
    </div>

    <div class="stat-row">
      <span class="stat-label">QR center-X</span>
      <span id="st-cx" class="stat-value">—</span>
    </div>

    <div class="btn-row">
      <button id="btn-start" onclick="navStart()">Start</button>
      <button id="btn-stop"  onclick="navStop()">Stop</button>
    </div>

    <h2 style="margin-top:6px">Log</h2>
    <div id="log-box" style="font-size:0.72rem; color:#666; height:140px; overflow-y:auto; font-family:monospace;"></div>
  </div>
</main>
<footer>Bug Navigator &mdash; camera-on-robot QR approach</footer>

<script>
  const badge  = document.getElementById('status-badge');
  const wsDot  = document.getElementById('ws-dot');
  const logBox = document.getElementById('log-box');

  function addLog(msg) {
    const line = document.createElement('div');
    line.textContent = new Date().toLocaleTimeString() + '  ' + msg;
    logBox.prepend(line);
    if (logBox.children.length > 60) logBox.lastChild.remove();
  }

  const BADGE_CLASSES = ['idle','searching','centering','approaching','arrived'];

  function applyNxt(connected) {
    const nb  = document.getElementById('nxt-badge');
    const dot = document.getElementById('nxt-dot');
    if (connected) {
      nb.className  = 'nxt-badge connected';
      nb.innerHTML  = '<span class="dot on" id="nxt-dot"></span>NXT conectado';
    } else {
      nb.className  = 'nxt-badge disconnected';
      nb.innerHTML  = '<span class="dot err" id="nxt-dot"></span>NXT desconectado';
    }
  }

  // Poll NXT status every 3 s in case it reconnects between state changes
  async function pollNxt() {
    try {
      const r = await fetch('/api/nxt-status');
      const d = await r.json();
      applyNxt(d.connected);
    } catch(_) {}
  }
  setInterval(pollNxt, 3000);
  pollNxt();

  function applyState(s) {
    BADGE_CLASSES.forEach(c => badge.classList.remove(c));
    badge.classList.add(s.status);
    badge.textContent = s.status;
    document.getElementById('st-status').textContent = s.status;
    document.getElementById('st-qr').textContent = s.qr_payload || '—';
    const pct = s.qr_area_pct ?? 0;
    document.getElementById('st-area').textContent = pct + ' %';
    document.getElementById('prox-bar').style.width = Math.min(pct * 6.67, 100) + '%';
    document.getElementById('st-cx').textContent = s.qr_cx != null
      ? s.qr_cx + ' px  (center ' + Math.round(s.frame_w/2) + ' px)'
      : '—';
    if (s.nxt_connected !== undefined) applyNxt(s.nxt_connected);
  }

  function connectWS() {
    const ws = new WebSocket('ws://' + location.host + '/ws/status');
    ws.onopen  = () => { wsDot.classList.add('on'); addLog('WebSocket connected'); };
    ws.onclose = () => { wsDot.classList.remove('on'); setTimeout(connectWS, 2000); };
    ws.onerror = () => ws.close();
    ws.onmessage = (e) => {
      const s = JSON.parse(e.data);
      applyState(s);
      addLog(s.status + (s.qr_payload ? '  QR=' + s.qr_payload : '') +
             (s.qr_area_pct != null ? '  ' + s.qr_area_pct + '%' : ''));
    };
  }
  connectWS();

  async function navStart() {
    const r = await fetch('/api/start', { method: 'POST' });
    addLog(r.ok ? 'Navigator started' : 'Start failed: ' + r.status);
  }
  async function navStop() {
    const r = await fetch('/api/stop', { method: 'POST' });
    addLog(r.ok ? 'Navigator stopped' : 'Stop failed: ' + r.status);
  }

  // Reload stream img on error (reconnect camera)
  document.getElementById('feed').onerror = function() {
    setTimeout(() => { this.src = '/stream?' + Date.now(); }, 1000);
  };
</script>
</body>
</html>
"""


# ── routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return _HTML


@app.get("/stream")
async def stream():
    """MJPEG stream — annotated frame from QRNavigator."""
    def generate():
        while True:
            frame = _navigator.get_annotated_frame() if _navigator else None
            if frame is None:
                # black placeholder if camera not ready yet
                frame = _placeholder_frame()
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ok:
                time.sleep(0.05)
                continue
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
            )
            time.sleep(0.04)   # ~25 fps

    return StreamingResponse(generate(), media_type="multipart/x-mixed-replace; boundary=frame")


def _placeholder_frame():
    import numpy as np
    img = np.zeros((240, 320, 3), dtype=np.uint8)
    cv2.putText(img, "waiting for camera...", (20, 120),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (80, 80, 80), 1)
    return img


@app.get("/api/nxt-status")
async def api_nxt_status():
    return {"connected": _nxt_connected()}


@app.get("/api/status")
async def api_status():
    if _navigator is None:
        return {"status": "idle"}
    s = _navigator.state
    return {
        "status":      s.status,
        "qr_payload":  s.qr_payload,
        "qr_cx":       s.qr_cx,
        "qr_area_pct": round(s.qr_area_pct * 100, 1) if s.qr_area_pct else None,
        "frame_w":     s.frame_w,
    }


@app.post("/api/start")
async def api_start():
    global _navigator, _nav_thread
    if _navigator and _navigator._running:
        return {"ok": False, "reason": "already running"}
    if _navigator:
        _navigator.stop()
        # Cerrar USB explícitamente antes de crear nuevo NXTDrive
        old_drive = getattr(_navigator, "_drive", None)
        if old_drive and hasattr(old_drive, "disconnect"):
            old_drive.disconnect()
        await asyncio.sleep(0.5)  # dar tiempo al USB para liberarse
    _navigator = build_navigator(_app_config, on_state_change=_on_state_change)
    _nav_thread = threading.Thread(target=_navigator.run, daemon=True, name="navigator")
    _nav_thread.start()
    log.info("Navigator started via API")
    return {"ok": True}


@app.post("/api/stop")
async def api_stop():
    if _navigator:
        _navigator.stop()
        log.info("Navigator stopped via API")
        return {"ok": True}
    return {"ok": False, "reason": "not running"}


@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket):
    await websocket.accept()
    async with _ws_lock:
        _ws_clients.add(websocket)
    # send current state immediately on connect
    if _navigator:
        s = _navigator.state
        await websocket.send_text(json.dumps({
            "status":        s.status,
            "qr_payload":    s.qr_payload,
            "qr_cx":         s.qr_cx,
            "qr_area_pct":   round(s.qr_area_pct * 100, 1) if s.qr_area_pct else None,
            "frame_w":       s.frame_w,
            "nxt_connected": _nxt_connected(),
        }))
    try:
        while True:
            await websocket.receive_text()   # keep-alive; we only push
    except WebSocketDisconnect:
        pass
    finally:
        async with _ws_lock:
            _ws_clients.discard(websocket)


# ── app startup/shutdown ──────────────────────────────────────────────────────
_app_config = None


@app.on_event("startup")
async def _startup():
    global _loop, _navigator, _nav_thread
    _loop = asyncio.get_event_loop()
    # Auto-start navigator when server boots
    _navigator = build_navigator(_app_config, on_state_change=_on_state_change)
    _nav_thread = threading.Thread(target=_navigator.run, daemon=True, name="navigator")
    _nav_thread.start()
    log.info("Navigator auto-started on server boot")


@app.on_event("shutdown")
async def _shutdown():
    if _navigator:
        _navigator.stop()


# ── CLI entry point ───────────────────────────────────────────────────────────
def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Bug Navigator web server")
    parser.add_argument("--config", default=None, help="Path to station_config.yaml")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    global _app_config
    _app_config = load_config(args.config) if args.config else load_config()

    log.info("Starting Bug Navigator at http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
