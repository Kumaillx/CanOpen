import json
import queue
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, Response, jsonify, send_from_directory

# Import the CANOpen script
import CANOpen

app = Flask(__name__)

DB_PATH = Path(__file__).with_name("canopen_cache.db")
CACHE_LIMIT = 20

# Thread-safe subscribers for SSE events
_subscribers = []
_subscribers_lock = threading.Lock()
_is_running = False
_automation_thread = None


def _connect_cache_db():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA user_version")
    except sqlite3.DatabaseError:
        try:
            conn.close()
        except Exception:
            pass
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_cache_db():
    with _connect_cache_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_cache (
                serial_number TEXT PRIMARY KEY,
                node_id INTEGER,
                device_type TEXT NOT NULL,
                status TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
            """
        )


def _serial_key(serial):
    return CANOpen.format_serial(serial)


def _store_snapshot(snapshot):
    seen_at = datetime.now().isoformat(timespec="microseconds")
    with _connect_cache_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_cache (
                serial_number TEXT PRIMARY KEY,
                node_id INTEGER,
                device_type TEXT NOT NULL,
                status TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
            """
        )

        for node in snapshot.boot_nodes:
            conn.execute(
                """
                INSERT INTO device_cache (serial_number, node_id, device_type, status, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(serial_number) DO UPDATE SET
                    node_id = excluded.node_id,
                    device_type = excluded.device_type,
                    status = excluded.status,
                    last_seen = excluded.last_seen
                """,
                (_serial_key(node.serial_number), None, node.device_type, "BOOT", seen_at),
            )

        for node in snapshot.canopen_nodes:
            conn.execute(
                """
                INSERT INTO device_cache (serial_number, node_id, device_type, status, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(serial_number) DO UPDATE SET
                    node_id = excluded.node_id,
                    device_type = excluded.device_type,
                    status = excluded.status,
                    last_seen = excluded.last_seen
                """,
                (
                    _serial_key(node.serial_number),
                    node.node_id,
                    node.device_type,
                    node.state,
                    seen_at,
                ),
            )

        row_count = conn.execute("SELECT COUNT(*) FROM device_cache").fetchone()[0]
        if row_count > CACHE_LIMIT:
            overflow = row_count - CACHE_LIMIT
            conn.execute(
                """
                DELETE FROM device_cache
                WHERE serial_number IN (
                    SELECT serial_number
                    FROM device_cache
                    ORDER BY last_seen ASC, serial_number ASC
                    LIMIT ?
                )
                """,
                (overflow,),
            )


def _load_cached_snapshot_payload():
    with _connect_cache_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_cache (
                serial_number TEXT PRIMARY KEY,
                node_id INTEGER,
                device_type TEXT NOT NULL,
                status TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
            """
        )
        rows = conn.execute(
            """
            SELECT serial_number, node_id, device_type, status
            FROM device_cache
            ORDER BY last_seen DESC, serial_number ASC
            """
        ).fetchall()

    boot_nodes = []
    canopen_nodes = []
    for row in rows:
        if row["node_id"] is None:
            boot_nodes.append({"serial": row["serial_number"], "type": row["device_type"]})
        else:
            canopen_nodes.append(
                {
                    "nodeId": row["node_id"],
                    "serial": row["serial_number"],
                    "state": row["status"],
                    "type": row["device_type"],
                }
            )

    return {"bootNodes": boot_nodes, "canOpenNodes": canopen_nodes}


_init_cache_db()

def _broadcast(event_type, payload):
    """Broadcasting events to the frontend dashboard."""
    data = json.dumps({
        "type": event_type,
        "ts": datetime.now().strftime("%H:%M:%S"),
        **payload
    })
    print(f"[WS-BROADCAST] Type: {event_type} | Data: {data}") # Easy print to check server console
    with _subscribers_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(data)
            except queue.Full:
                _subscribers.remove(q)

# Override the log and get_snapshot functions in CANOpen
_original_log = CANOpen.log
def _web_log(level, message):
    # Print to the local terminal first
    _original_log(level, message)
    # Broadcast to the browser log
    _broadcast("log", {"level": level.upper(), "message": message})
    
    # Simple Stage detection based on logs
    msg_up = message.upper()
    if "STAGE 0" in msg_up:
        _broadcast("stage", {"stage": 0})
    elif "STAGE 1" in msg_up:
        _broadcast("stage", {"stage": 1})
    elif "STAGE 2" in msg_up:
        _broadcast("stage", {"stage": 2})

CANOpen.log = _web_log

_original_get_snapshot = CANOpen.get_snapshot
def _web_get_snapshot():
    snap = _original_get_snapshot()
    _store_snapshot(snap)
    _broadcast("snapshot", {
        **_load_cached_snapshot_payload()
    })
    return snap

CANOpen.get_snapshot = _web_get_snapshot

def _run_automation():
    global _is_running
    print("[SERVER] Starting CANOpen automation main run loop in background thread...")
    try:
        CANOpen.main()
        _broadcast("done", {"success": True, "message": "READY FOR PUMPING"})
    except Exception as e:
        print(f"[SERVER] Exception occurred during automation run: {e}")
        _broadcast("done", {"success": False, "message": str(e)})
    finally:
        _is_running = False
        _broadcast("status", {"running": False})
        print("[SERVER] CANOpen automation run loop finished.")

@app.route("/")
def index():
    print("[SERVER] Index page requested.")
    return send_from_directory(".", "index.html")

@app.route("/api/start", methods=["POST"])
def api_start():
    global _is_running, _automation_thread
    print("[SERVER] /api/start endpoint hit!")
    if _is_running:
        print("[SERVER] /api/start requested but automation is already running.")
        return jsonify({"status": "already_running"}), 409
    
    _is_running = True
    _automation_thread = threading.Thread(target=_run_automation, daemon=True)
    _automation_thread.start()
    return jsonify({"status": "started"})

@app.route("/api/status")
def api_status():
    return jsonify({"running": _is_running})

@app.route("/events")
def sse_stream():
    print("[SERVER] New SSE subscriber connected to /events.")
    q = queue.Queue(maxsize=500)
    with _subscribers_lock:
        _subscribers.append(q)

    def generate():
        try:
            yield 'data: {"type":"connected"}\n\n'
            yield f"data: {json.dumps({'type': 'snapshot', 'ts': datetime.now().strftime('%H:%M:%S'), **_load_cached_snapshot_payload()})}\n\n"
            while True:
                try:
                    data = q.get(timeout=10)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            print("[SERVER] SSE subscriber disconnected.")
            with _subscribers_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
    )

if __name__ == "__main__":
    print("\n=========================================")
    print("  SIMPLE DEBUG ENGINE RUNNING")
    print("  Go to: http://localhost:5000")
    print("=========================================\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
