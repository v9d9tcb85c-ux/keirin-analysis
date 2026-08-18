# -*- coding: utf-8 -*-
from flask import Flask, jsonify, send_from_directory
from pathlib import Path
import threading
import time
from engine import scan_today

BASE_DIR = Path(__file__).resolve().parent
STATIC = BASE_DIR / "static"
app = Flask(__name__, static_folder=str(STATIC), static_url_path="")

lock = threading.Lock()
state = {
    "running": False,
    "phase": "idle",
    "current": "",
    "done": 0,
    "total": 0,
    "last_update": 0,
    "date": "",
    "matches": [],
    "checked_races": 0,
    "unpublished": [],
    "errors": [],
    "message": ""
}

CACHE_SECONDS = 600

def update_progress(p):
    with lock:
        state["phase"] = p.get("phase", state["phase"])
        state["current"] = p.get("current", state["current"])
        state["done"] = p.get("done", state["done"])
        state["total"] = p.get("total", state["total"])

def run_scan():
    with lock:
        if state["running"]:
            return
        state.update({
            "running": True, "phase":"starting", "current":"準備中",
            "done":0, "total":0, "message":""
        })
    try:
        result = scan_today(update_progress)
        with lock:
            state["date"] = result["date"]
            state["matches"] = result["matches"]
            state["checked_races"] = result["checked_races"]
            state["unpublished"] = result.get("unpublished", [])
            state["errors"] = result["errors"]
            state["last_update"] = int(time.time())
            state["phase"] = "done"
            state["current"] = "完了"
            state["message"] = f"該当 {len(result['matches'])}件 / 未公開 {len(result.get('unpublished', []))}件"
    except Exception as e:
        with lock:
            state["phase"] = "error"
            state["message"] = str(e)
    finally:
        with lock:
            state["running"] = False

def start_scan(force=False):
    with lock:
        running = state["running"]
        fresh = state["last_update"] and (time.time() - state["last_update"] < CACHE_SECONDS)
    if running:
        return False, "running"
    if fresh and not force:
        return False, "fresh"
    threading.Thread(target=run_scan, daemon=True).start()
    return True, "started"

@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")

@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(STATIC, "manifest.webmanifest")

@app.route("/sw.js")
def sw():
    return send_from_directory(STATIC, "sw.js")

@app.route("/api/status")
def status():
    with lock:
        data = dict(state)
    return jsonify(data)

@app.route("/api/search", methods=["POST"])
def search():
    started, reason = start_scan(force=True)
    return jsonify({"started": started, "reason": reason})

@app.route("/api/auto", methods=["POST"])
def auto():
    started, reason = start_scan(force=False)
    return jsonify({"started": started, "reason": reason})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
