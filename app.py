from flask import Flask, jsonify, request, send_from_directory
from pathlib import Path
from threading import Lock
import os, time, uuid

app = Flask(__name__)
BASE = Path(__file__).resolve().parent
lock = Lock()

AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "").strip()
CONTROL_KEY = os.environ.get("CONTROL_KEY", "").strip()

state = {
    "agent_online": False,
    "agent_last_seen": 0.0,
    "phase": "idle",
    "running": False,
    "current": "",
    "detail": "",
    "venues_info": [],
    "matches": [],
    "errors": [],
    "counters": {},
    "pending_command": None,
    "active_command_id": None,
    "updated_at": time.time(),
}

def now():
    return time.time()

def control_ok():
    if not CONTROL_KEY:
        return True
    got = request.headers.get("X-Control-Key", "") or request.args.get("key", "")
    return got == CONTROL_KEY

def agent_ok():
    if not AGENT_TOKEN:
        return False
    return request.headers.get("X-Agent-Token", "") == AGENT_TOKEN

def public_state():
    with lock:
        s = dict(state)
        s["agent_online"] = bool(s["agent_last_seen"] and now() - s["agent_last_seen"] < 12)
        s.pop("pending_command", None)
        return s

def enqueue(kind, payload=None):
    with lock:
        if state["pending_command"] is not None:
            return None
        cid = uuid.uuid4().hex
        state["pending_command"] = {
            "id": cid,
            "kind": kind,
            "payload": payload or {},
            "created_at": now(),
        }
        state["active_command_id"] = cid
        state["updated_at"] = now()
        return cid

@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")

@app.get("/api/status")
def status():
    return jsonify(public_state())

@app.post("/api/control/load-board")
def load_board():
    if not control_ok():
        return jsonify(ok=False, reason="unauthorized"), 401
    s = public_state()
    if not s["agent_online"]:
        return jsonify(ok=False, reason="pc_offline"), 409
    if s["running"]:
        return jsonify(ok=False, reason="running"), 409
    cid = enqueue("load_board")
    if not cid:
        return jsonify(ok=False, reason="command_pending"), 409
    with lock:
        state.update({
            "running": True,
            "phase": "queued",
            "current": "PCへ開催場取得を依頼中",
            "detail": "PC側の取得エンジンが開始するのを待っています。",
            "venues_info": [],
            "matches": [],
            "errors": [],
            "counters": {},
        })
    return jsonify(ok=True, command_id=cid)

@app.post("/api/control/search")
def search():
    if not control_ok():
        return jsonify(ok=False, reason="unauthorized"), 401
    s = public_state()
    if not s["agent_online"]:
        return jsonify(ok=False, reason="pc_offline"), 409
    if s["running"]:
        return jsonify(ok=False, reason="running"), 409
    selected = (request.get_json(silent=True) or {}).get("selected") or []
    valid = {x.get("slug") for x in s.get("venues_info", [])}
    selected = [x for x in selected if x in valid]
    if not selected:
        return jsonify(ok=False, reason="no_selection"), 400
    cid = enqueue("search_selected", {"selected": selected})
    if not cid:
        return jsonify(ok=False, reason="command_pending"), 409
    with lock:
        state.update({
            "running": True,
            "phase": "queued",
            "current": "PCへ検索を依頼中",
            "detail": f"選択した{len(selected)}場をPC側で検索します。",
            "matches": [],
            "errors": [],
        })
    return jsonify(ok=True, command_id=cid)

@app.post("/api/control/stop")
def stop():
    if not control_ok():
        return jsonify(ok=False, reason="unauthorized"), 401
    cid = enqueue("stop")
    if not cid:
        # Stop is important: overwrite a stale non-stop command if necessary.
        with lock:
            cid = uuid.uuid4().hex
            state["pending_command"] = {"id": cid, "kind": "stop", "payload": {}, "created_at": now()}
    return jsonify(ok=True, command_id=cid)

@app.post("/api/control/reset")
def reset():
    if not control_ok():
        return jsonify(ok=False, reason="unauthorized"), 401
    with lock:
        if state["running"]:
            return jsonify(ok=False, reason="running"), 409
        state.update({
            "phase": "idle", "running": False, "current": "", "detail": "",
            "venues_info": [], "matches": [], "errors": [], "counters": {},
            "updated_at": now(),
        })
    return jsonify(ok=True)

@app.get("/api/agent/next")
def agent_next():
    if not agent_ok():
        return jsonify(ok=False), 401
    with lock:
        state["agent_last_seen"] = now()
        cmd = state["pending_command"]
        state["pending_command"] = None
        state["updated_at"] = now()
    return jsonify(ok=True, command=cmd)

@app.post("/api/agent/progress")
def agent_progress():
    if not agent_ok():
        return jsonify(ok=False), 401
    data = request.get_json(silent=True) or {}
    with lock:
        state["agent_last_seen"] = now()
        for k in ("phase","running","current","detail","venues_info","matches","errors","counters"):
            if k in data:
                state[k] = data[k]
        state["updated_at"] = now()
    return jsonify(ok=True)

@app.post("/api/agent/finish")
def agent_finish():
    if not agent_ok():
        return jsonify(ok=False), 401
    data = request.get_json(silent=True) or {}
    with lock:
        state["agent_last_seen"] = now()
        for k in ("phase","current","detail","venues_info","matches","errors","counters"):
            if k in data:
                state[k] = data[k]
        state["running"] = False
        state["updated_at"] = now()
    return jsonify(ok=True)

@app.get("/health")
def health():
    return jsonify(ok=True, role="relay-only", browser="none")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
