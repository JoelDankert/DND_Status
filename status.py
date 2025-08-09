# statusboard_bouncing.py
# Vollbild-Statusboard mit langsamem DVD-Logo-Bounce-Effekt.
# - Emoji + Text, live sync via SSE
# - Globaler Hotkey: F9 -> nächster Modus (notify-send unter Linux)
# - Browser: 1–4 setzt Modus, Doppelklick/‑tap -> Vollbild
# - Inhalt "schwebt" langsam über den Bildschirm und prallt an den Kanten ab
# - Hintergrund: dunklere Volltonfarbe, Karte in Originalfarbe

from flask import Flask, Response, render_template_string
from queue import Queue
import socket
import threading
import subprocess
from pynput import keyboard

app = Flask(__name__)

MODES = {
    "1": {"title": "Nicht Stören<br>Lieber schreiben",    "note": "ungern unterbrechen", "emoji": "⛔",  "color": "#D32F2F"},
    "2": {"title": "Im Anruf<br>Nicht stören",            "note": "ungern unterbrechen", "emoji": "🔇",  "color": "#F57C00"},
    "3": {"title": "Im Anruf<br>Bitte klopfen",           "note": "gerne unterbrechen",  "emoji": "🔇",  "color": "#FBC02D"},
    "4": {"title": "Verfügbar",                           "note": "gerne unterbrechen",  "emoji": "✅",  "color": "#388E3C"},
    "5": {"title": "Bin kurz Weg",                        "note": "gleich wieder da",    "emoji": "🫠",   "color": "#D1D1D1"},
}

DEFAULT_MODE = "1"
HOST, PORT = "0.0.0.0", 8000

current_mode = DEFAULT_MODE
subscribers = set()
subs_lock = threading.Lock()

def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def serialize_mode():
    m = MODES[current_mode]
    def esc(s): return s.replace("\\","\\\\").replace('"','\\"')
    return (
        f'{{"key":"{esc(current_mode)}",'
        f'"title":"{esc(m["title"])}",'
        f'"note":"{esc(m["note"])}",'
        f'"emoji":"{esc(m["emoji"])}",'
        f'"color":"{esc(m["color"])}"}}'
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

def cycle_mode():
    global current_mode
    keys = sorted(MODES.keys(), key=int)
    idx = keys.index(current_mode)
    current_mode = keys[(idx + 1) % len(keys)]
    publish()
    try:
        subprocess.run(["notify-send", f"Modus {current_mode}", MODES[current_mode]["title"].replace("<br>", " · ")], check=False)
    except FileNotFoundError:
        pass

def sse_stream():
    q = Queue()
    with subs_lock:
        subscribers.add(q)
    q.put_nowait(serialize_mode())
    try:
        while True:
            data = q.get()
            yield f"data: {data}\n\n"
    finally:
        with subs_lock:
            subscribers.discard(q)

@app.get("/")
def index():
    init_color = MODES[current_mode]["color"]
    return render_template_string("""
<!doctype html>
<html lang=\"de\">
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Status</title>
<style>
  :root { --bg: {{ init_color }}; }
  html, body {
    height: 100%; margin: 0;
    background-color: color-mix(in srgb, var(--bg) 0%, black);
    font-family: system-ui, sans-serif;
    overflow: hidden;
    color: white; text-align: center;
    transition: background-color .25s ease;
  }
  .float {
    position: fixed; left: 0; top: 0;
    will-change: transform;
  }
  .card {
    background-color: var(--bg);
    padding: 2rem 3rem; border-radius: 1.5rem;
    box-shadow: 0 8px 24px rgba(0,0,0,0.35);
  }
  .emoji { font-size: min(32vw, 36vh); margin-bottom: 2rem; white-space: nowrap; }
  h1 { font-size: clamp(2rem, 5vw, 4rem); margin: 0 0 1rem 0; }
  p  { font-size: clamp(1rem, 3vw, 2rem); margin: 0; opacity: 0.85; }
</style>

<div class=\"float\" id=\"float\">
  <div class=\"card\">
    <div class=\"emoji\" id=\"emoji\">⏳</div>
    <h1 id=\"title\">Lade Status…</h1>
    <p id=\"note\">Bitte warten.</p>
  </div>
</div>

<script>
  const emojiEl = document.getElementById("emoji");
  const titleEl = document.getElementById("title");
  const noteEl  = document.getElementById("note");
  const floatEl = document.getElementById("float");
  const es = new EventSource("/events");

  function applyMode(m){
    document.documentElement.style.setProperty('--bg', m.color);
    emojiEl.textContent = m.emoji;
    titleEl.innerHTML  = m.title;
    noteEl.textContent = m.note;
    flashEdgeHit = false;
  }
  es.onmessage = ev => { try { applyMode(JSON.parse(ev.data)); } catch(e) {} };

  let askedFs = false;
  async function ensureFullscreen(){
    if (askedFs) return;
    askedFs = true;
    if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
      try { await document.documentElement.requestFullscreen(); } catch {}
    }
  }

  window.addEventListener("keydown", ev => {
    if (/^[1-4]$/.test(ev.key)) {
      ensureFullscreen();
      fetch("/set/" + encodeURIComponent(ev.key), {method:"POST"}).catch(()=>{});
    }
  });
  window.addEventListener("dblclick", () => ensureFullscreen());
  let lastTap = 0;
  window.addEventListener("touchend", () => {
    const now = Date.now();
    if (now - lastTap < 300) ensureFullscreen();
    lastTap = now;
  }, {passive: true});

  let x = 80, y = 60;
  let vx = 28, vy = 22;
  const MIN_SPEED = 18;
  const MAX_SPEED = 60;
  let lastTime = performance.now();
  let flashEdgeHit = false;

  function sizeFloatBox(){
    const rect = floatEl.getBoundingClientRect();
    return { w: rect.width, h: rect.height };
  }

  function step(now){
    const dt = Math.max(0.001, (now - lastTime) / 1000);
    lastTime = now;
    const W = window.innerWidth;
    const H = window.innerHeight;
    const box = sizeFloatBox();
    x += vx * dt;
    y += vy * dt;
    if (x <= 0) { x = 0; vx = Math.abs(vx); edgeHit(); }
    else if (x + box.w >= W) { x = W - box.w; vx = -Math.abs(vx); edgeHit(); }
    if (y <= 0) { y = 0; vy = Math.abs(vy); edgeHit(); }
    else if (y + box.h >= H) { y = H - box.h; vy = -Math.abs(vy); edgeHit(); }
    floatEl.style.transform = `translate(${x}px, ${y}px)`;
    requestAnimationFrame(step);
  }

  function edgeHit(){
    if (!flashEdgeHit) return;
    //flashEdgeHit = false;
    floatEl.animate([
      { filter: 'brightness(1.0)' },
      { filter: 'brightness(1.18)' },
      { filter: 'brightness(1.0)' }
    ], { duration: 320 });
  }

  window.addEventListener('resize', () => {
    const box = sizeFloatBox();
    const W = window.innerWidth, H = window.innerHeight;
    x = Math.min(Math.max(0, x), Math.max(0, W - box.w));
    y = Math.min(Math.max(0, y), Math.max(0, H - box.h));
    const sp = Math.hypot(vx, vy);
    if (sp < MIN_SPEED) {
      const f = MIN_SPEED / Math.max(1e-3, sp);
      vx *= f; vy *= f;
    } else if (sp > MAX_SPEED) {
      const f = MAX_SPEED / sp; vx *= f; vy *= f;
    }
  });

  requestAnimationFrame(step);
</script>
""", init_color=init_color)

@app.get("/events")
def events():
    return Response(sse_stream(), mimetype="text/event-stream")

@app.post("/set/<key>")
def set_mode(key):
    global current_mode
    if key not in MODES:
        return ("unknown mode", 400)
    current_mode = key
    publish()
    return ("", 204)

def hotkey_listener():
    pressed = set()
    def on_press(key):
        if key == keyboard.Key.f9 and key not in pressed:
            pressed.add(key)
            cycle_mode()
    def on_release(key):
        if key in pressed:
            pressed.remove(key)
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

if __name__ == "__main__":
    threading.Thread(target=hotkey_listener, daemon=True).start()
    lan_ip = get_lan_ip()
    print("Statusboard läuft (Bouncing):")
    print(f"  lokal:   http://localhost:{PORT}")
    print(f"  im LAN:  http://{lan_ip}:{PORT}")
    print("Hotkey: F9 = nächster Modus | Browser: 1–4 setzen, Doppelklick/-tap = Vollbild")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
