import json
import queue
import threading
import time
from datetime import datetime
from flask import Flask, Response, jsonify, send_from_directory

# Import the CANOpen script
import CANOpen

app = Flask(__name__)

# Thread-safe subscribers for SSE events
_subscribers = []
_subscribers_lock = threading.Lock()
_is_running = False
_automation_thread = None

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
    _broadcast("snapshot", {
        "bootNodes": [
            {"serial": CANOpen.format_serial(n.serial_number), "type": n.device_type}
            for n in snap.boot_nodes
        ],
        "canOpenNodes": [
            {
                "nodeId": n.node_id,
                "serial": CANOpen.format_serial(n.serial_number),
                "state": n.state,
                "type": n.device_type
            }
            for n in snap.canopen_nodes
        ]
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
