from flask import Flask, jsonify, send_from_directory, request
from pathlib import Path
import threading
import os

from engine import load_today_board, scan_selected, ENGINE_VERSION

app = Flask(__name__)
BASE_DIR = Path(__file__).parent
lock = threading.Lock()
stop_event = threading.Event()

state = {
    "running": False,
    "phase": "idle",
    "current": "",
    "detail": "",
    "venues_info": [],
    "matches": [],
    "errors": [],
    "counters": {},
}


def progress(payload):
    with lock:
        for key, value in payload.items():
            if key == "counters":
                state["counters"] = value
            elif key in state:
                state[key] = value


def board_job():
    try:
        result = load_today_board(progress, stop_event)
        with lock:
            state.update({
                "venues_info": result.get("venues_info", []),
                "counters": result.get("counters", {}),
                "errors": result.get("errors", []),
                "phase": "stopped" if result.get("stopped") else "select",
                "current": "途中停止" if result.get("stopped") else "開催場を選択",
                "detail": "F2など検索したい開催場にチェックしてください。",
            })
    except Exception as exc:
        with lock:
            state.update({
                "phase": "error",
                "current": "開催場取得エラー",
                "errors": [f"{type(exc).__name__}: {exc}"],
            })
    finally:
        with lock:
            state["running"] = False


def search_job(selected):
    try:
        with lock:
            board = list(state["venues_info"])
        result = scan_selected(selected, board, progress, stop_event)
        with lock:
            state.update({
                "matches": result.get("matches", []),
                "errors": result.get("errors", []),
                "counters": result.get("counters", {}),
                "phase": "stopped" if result.get("stopped") else "done",
                "current": "途中停止" if result.get("stopped") else "検索完了",
                "detail": "停止しました。" if result.get("stopped") else "選択した開催場の検索が完了しました。",
            })
    except Exception as exc:
        with lock:
            state.update({
                "phase": "error",
                "current": "検索エラー",
                "errors": [f"{type(exc).__name__}: {exc}"],
            })
    finally:
        with lock:
            state["running"] = False


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/status")
def status():
    with lock:
        return jsonify(dict(state))


@app.post("/api/load-venues")
def load_venues():
    with lock:
        if state["running"]:
            return jsonify(ok=False, reason="running"), 409
        state.update({
            "running": True,
            "phase": "board",
            "current": "今日の開催場を取得中",
            "detail": "WINTICKETの開催一覧1ページを確認しています。",
            "venues_info": [],
            "matches": [],
            "errors": [],
            "counters": {},
        })
        stop_event.clear()
    threading.Thread(target=board_job, daemon=True, name="board-job").start()
    return jsonify(ok=True)


@app.post("/api/search-selected")
def search_selected():
    selected = (request.get_json(silent=True) or {}).get("selected") or []
    with lock:
        if state["running"]:
            return jsonify(ok=False, reason="running"), 409
        valid = {item.get("slug") for item in state["venues_info"]}
        selected = [slug for slug in selected if slug in valid]
        if not selected:
            return jsonify(ok=False, reason="no_selection"), 400
        state.update({
            "running": True,
            "phase": "races",
            "current": "検索準備中",
            "detail": f"選択した{len(selected)}場だけを検索します。",
            "matches": [],
            "errors": [],
        })
        stop_event.clear()
    threading.Thread(target=search_job, args=(selected,), daemon=True, name="search-job").start()
    return jsonify(ok=True)


@app.post("/api/stop")
def stop():
    stop_event.set()
    return jsonify(ok=True)


@app.post("/api/reset")
def reset():
    with lock:
        if state["running"]:
            return jsonify(ok=False, reason="running"), 409
        state.update({
            "running": False,
            "phase": "idle",
            "current": "",
            "detail": "",
            "venues_info": [],
            "matches": [],
            "errors": [],
            "counters": {},
        })
    return jsonify(ok=True)


@app.get("/health")
def health():
    return jsonify(ok=True, app_version="mobile-select-v2.1", engine_version=ENGINE_VERSION)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
