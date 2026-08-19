# -*- coding: utf-8 -*-
from flask import Flask, jsonify, send_from_directory
from pathlib import Path
import json
import os
import threading
import time
from engine import scan_today

BASE_DIR = Path(__file__).resolve().parent
STATIC = BASE_DIR
CACHE_FILE = Path(os.environ.get("WINTICKET_STATE_FILE", "/tmp/winticket_last_result.json"))
CACHE_SECONDS = 600

app = Flask(__name__, static_folder=str(STATIC), static_url_path="")
lock = threading.Lock()


def _empty_state():
    return {
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
        "message": "",
    }


def _load_cache():
    s = _empty_state()
    try:
        if CACHE_FILE.exists():
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            for k in s:
                if k in data:
                    s[k] = data[k]
            # A previous process may have died while marked running.
            s["running"] = False
            if s.get("last_update"):
                s["phase"] = "done"
                s["current"] = "完了"
    except Exception as e:
        print(f"[APP] cache load failed: {type(e).__name__}: {e}", flush=True)
    return s


state = _load_cache()


def _save_cache_unlocked():
    """Best-effort local cache. Client localStorage is the second safety net."""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        payload = dict(state)
        payload["running"] = False
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception as e:
        print(f"[APP] cache save failed: {type(e).__name__}: {e}", flush=True)


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
        # Keep the previous successful result visible while a new scan runs.
        state.update({
            "running": True,
            "phase": "starting",
            "current": "準備中",
            "done": 0,
            "total": 0,
            "message": "",
            "errors": [],
        })

    try:
        result = scan_today(update_progress)
        with lock:
            state["date"] = result.get("date", "")
            state["matches"] = result.get("matches", [])
            state["checked_races"] = int(result.get("checked_races", 0) or 0)
            state["unpublished"] = result.get("unpublished", [])
            state["errors"] = result.get("errors", [])
            state["last_update"] = int(time.time())
            state["phase"] = "done"
            state["current"] = "完了"
            state["done"] = state.get("total", 0)
            state["message"] = (
                f"該当 {len(state['matches'])}件 / "
                f"未公開 {len(state['unpublished'])}件 / "
                f"取得エラー {len(state['errors'])}件"
            )
            _save_cache_unlocked()
    except Exception as e:
        print(f"[APP] scan failed: {type(e).__name__}: {e}", flush=True)
        with lock:
            # Do not erase the last successful result on a scan failure.
            state["phase"] = "error"
            state["current"] = "エラー"
            state["message"] = f"{type(e).__name__}: {e}"
    finally:
        with lock:
            state["running"] = False


def start_scan(force=False):
    with lock:
        running = bool(state["running"])
        fresh = bool(state["last_update"]) and (time.time() - state["last_update"] < CACHE_SECONDS)
    if running:
        return False, "running"
    if fresh and not force:
        return False, "fresh"
    threading.Thread(target=run_scan, daemon=True, name="winticket-scan").start()
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


@app.route("/health")
def health():
    return jsonify({"ok": True, "running": bool(state.get("running"))})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
