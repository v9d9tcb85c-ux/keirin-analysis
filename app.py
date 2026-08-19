# -*- coding: utf-8 -*-
from flask import Flask, jsonify, send_from_directory, make_response
from pathlib import Path
import os
import threading
import time

from engine import scan_today

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)

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
    "unpublished": [],
    "checked_races": 0,
    "errors": [],
    "message": "",
}

CACHE_SECONDS = 600


def _no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def update_progress(progress):
    if not isinstance(progress, dict):
        return
    with lock:
        state["phase"] = progress.get("phase", state["phase"])
        state["current"] = progress.get("current", state["current"])
        state["done"] = progress.get("done", state["done"])
        state["total"] = progress.get("total", state["total"])


def run_scan():
    with lock:
        if state["running"]:
            return
        state.update({
            "running": True,
            "phase": "starting",
            "current": "準備中",
            "done": 0,
            "total": 0,
            "message": "",
        })

    try:
        result = scan_today(update_progress)

        if not isinstance(result, dict):
            raise RuntimeError("engine.scan_today() returned an invalid result")

        with lock:
            state["date"] = result.get("date", "")
            state["matches"] = result.get("matches", [])
            state["unpublished"] = result.get("unpublished", [])
            state["checked_races"] = result.get("checked_races", 0)
            state["errors"] = result.get("errors", [])
            state["last_update"] = int(time.time())
            state["phase"] = "done"
            state["current"] = "完了"
            state["message"] = f"該当 {len(state['matches'])}件"

    except Exception as exc:
        with lock:
            state["phase"] = "error"
            state["current"] = "エラー"
            state["errors"] = list(state.get("errors", [])) + [
                f"{type(exc).__name__}: {exc}"
            ]
            state["message"] = f"{type(exc).__name__}: {exc}"

        print(f"[APP] SCAN_ERROR {type(exc).__name__}: {exc}", flush=True)

    finally:
        with lock:
            state["running"] = False


def start_scan(force=False):
    with lock:
        if state["running"]:
            return False, "running"

        fresh = bool(
            state["last_update"]
            and (time.time() - state["last_update"] < CACHE_SECONDS)
        )

    if fresh and not force:
        return False, "fresh"

    threading.Thread(target=run_scan, daemon=True).start()
    return True, "started"


@app.route("/")
def index():
    response = make_response(send_from_directory(BASE_DIR, "index.html"))
    return _no_cache(response)


@app.route("/api/status")
def api_status():
    with lock:
        data = dict(state)
    return _no_cache(jsonify(data))


@app.route("/api/search", methods=["POST"])
def api_search():
    started, reason = start_scan(force=True)
    return _no_cache(jsonify({"started": started, "reason": reason}))


@app.route("/api/auto", methods=["POST"])
def api_auto():
    started, reason = start_scan(force=False)
    return _no_cache(jsonify({"started": started, "reason": reason}))


@app.route("/health")
def health():
    with lock:
        running = state["running"]
    return _no_cache(jsonify({"ok": True, "running": running}))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
