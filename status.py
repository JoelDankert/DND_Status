from flask import Flask, Response, send_from_directory
from queue import Queue
import threading
import json
import os
import socket
import subprocess
import re
import shutil
import time

# --- Hotkey-Unterstützung nur aktivieren, wenn möglich ---
hotkeys_available = True
try:
    from pynput import keyboard
except Exception as e:
    hotkeys_available = False
    print(f"[Hinweis] Hotkeys deaktiviert: {e}")

# -------- config / data --------
HERE = os.path.dirname(os.path.abspath(__file__))
APPS_JSON_PATH = os.path.join(HERE, "apps.json")

with open(APPS_JSON_PATH, "r", encoding="utf-8") as f:
    data = json.load(f)
MODES = data["modes"]
DEFAULT_MODE = data.get("default_mode", "1")

HOST, PORT = "0.0.0.0", 8000

# -------- state & helpers --------
current_mode = DEFAULT_MODE
subscribers = set()
subs_lock = threading.Lock()
dnd = False

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"','\\"')

def serialize_mode() -> str:
    m = MODES[current_mode]
    return (
        f'{{"key":"{_esc(current_mode)}",'
        f'"title":"{_esc(m["title"])}",'
        f'"note":"{_esc(m["note"])}",'
        f'"emoji":"{_esc(m["emoji"])}",'
        f'"color":"{_esc(m["color"])}"}}'
    )

def publish():
    payload = serialize_mode()
    with subs_lock:
        dead = []
        for q in list(subscribers):
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            subscribers.discard(q)

def register_subscriber() -> Queue:
    q = Queue()
    with subs_lock:
        subscribers.add(q)
    q.put_nowait(serialize_mode())
    return q

def unregister_subscriber(q: Queue):
    with subs_lock:
        subscribers.discard(q)

def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def cycle_dnd():
    global dnd
    dnd = not dnd
    
    try:
        if dnd:
            subprocess.run(["notify-send", "Bitte nicht stören aktiviert", "Status wird geändert"], check=False)
        else:
            subprocess.run(["notify-send", "Bitte nicht stören deaktiviert", "Status wird geändert"], check=False)
    except FileNotFoundError:
        pass

def hotkey_listener():
    if not hotkeys_available:
        return
    pressed = set()
    def on_press(key):
        if key == keyboard.Key.f9 and key not in pressed:
            pressed.add(key)
            cycle_dnd()
    def on_release(key):
        if key in pressed:
            pressed.remove(key)
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

    
# events

def set_mode_event(event):
    global current_mode
    
    for mode in MODES:
        curr = MODES[mode]
        if event in curr["event"]:
            set_mode(mode)
            
            try:
                subprocess.run(["notify-send", str(MODES[current_mode]["emoji"])+" (event)", MODES[current_mode]["title"].replace("<br>", " · ")], check=False)
            except FileNotFoundError:
                pass
            

def event_listener():
    def run(cmd):
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
            return out.decode("utf-8", errors="ignore")
        except Exception:
            return ""

    def cmd_exists(name):
        return shutil.which(name) is not None

    # --- Detect Discord voice call via PulseAudio/PipeWire (pactl) ---
    def in_discord_call():
        if not cmd_exists("pactl"):
            return False

        so = run(["pactl", "list", "source-outputs"])
        si = run(["pactl", "list", "sink-inputs"])
        text = so + "\n" + si
        blocks = re.split(r"\n(?=\S)", text)

        def block_has_discord(b):
            return ("application.name = \"Discord\"" in b
                    or "application.process.binary = \"Discord\"" in b
                    or re.search(r"^.*Discord.*$", b, re.IGNORECASE | re.MULTILINE))

        def block_running(b):
            return ("State: RUNNING" in b) or ("Corked: no" in b)

        for b in blocks:
            if block_has_discord(b) and block_running(b):
                return True

        # Fallback: if any source-output mentions Discord at all, assume in-call
        if "source-outputs" in so and ("Discord" in so):
            return True

        return False

    # --- Idle time (AFK) detection ---
    def get_idle_seconds():
        # X11: xprintidle (milliseconds)
        if cmd_exists("xprintidle"):
            out = run(["xprintidle"]).strip()
            if out.isdigit():
                return int(out) / 1000.0

        return None

    # --- Any media playing now? (MPRIS via playerctl) ---
    def media_playing_now():
        if cmd_exists("playerctl"):
            statuses = run(["playerctl", "-a", "status"])
            for line in statuses.splitlines():
                if line.strip().lower() == "playing":
                    return True
            return False

        return False

    last_event = None

    while True:
        event = 1  # default "no other" -> "Verfügbar"
        try:
            if in_discord_call():
                event = 3
            else:
                idle = get_idle_seconds()
                if (idle is not None) and (idle >= 30) and (not media_playing_now()):
                    event = 2
        except Exception:
            event = 1

        if dnd: 
            event += 100
        
        if event != last_event:
            try:    
                set_mode_event(event)
                last_event = event
            except Exception:
                pass

        time.sleep(2)  # poll interval


# -------- app --------
app = Flask(__name__, static_folder='.', static_url_path='')

@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")

@app.get("/events")
def events():
    def stream():
        q = register_subscriber()
        try:
            while True:
                data = q.get()
                yield f"data: {data}\n\n"
        finally:
            unregister_subscriber(q)
    return Response(stream(), mimetype="text/event-stream")

@app.post("/set/<key>")
def set_mode(key):
    global current_mode
    if key not in MODES:
        return ("unknown mode", 400)
    current_mode = key
    publish()
    return ("", 204)

if __name__ == "__main__":
    if hotkeys_available:
        threading.Thread(target=hotkey_listener, daemon=True).start()
    else:
        print("Hotkey F9 ist deaktiviert (kein DISPLAY/X verfügbar).")
    threading.Thread(target=event_listener, daemon=True).start()
    lan_ip = get_lan_ip()
    print("Statusboard läuft (Bouncing):")
    print(f"  lokal:   http://localhost:{PORT}")
    print(f"  im LAN:  http://{lan_ip}:{PORT}")
    
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


