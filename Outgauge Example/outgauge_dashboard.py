#!/usr/bin/env python3
"""
ErinsMod OutGauge API Dashboard
----------------------------------------------------
Run:
    python outgauge_dashboard_round.py
Open on LAN:
    http://<LAN_IP>:8080/
Stop:
    Ctrl+C
----------------------------------------------------
"""

import socket
import struct
import json
import threading
import time
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

BIND_ADDR_HTTP = "0.0.0.0"
HTTP_PORT = 8080

JSON_PORT = 9998
BIN_PORT  = 9999
BIND_ADDR_UDP = "127.0.0.1"

BROADCAST_HZ = 20  # SSE push rate

# ------------- Shared telemetry -------------
latest_lock = threading.Lock()
latest = None  # dict with keys: time, car, rpm, speed, turbo, etc.

clients_lock = threading.Lock()
clients = set()  # set of file-like objects (wfile) for SSE


def now_str():
    return time.strftime("%H:%M:%S", time.localtime())

def get_lan_ip_hint():
    """Best-effort: get LAN IP, preferring 192.168.1.* style if present."""
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
    return ip


# ------------- OutGauge binary parsing -------------
_BASE_FMT = "<I4sHBB7fII3f16s16s"   # 92 bytes
_BASE_LEN = struct.calcsize(_BASE_FMT)


def parse_outgauge_packet(b: bytes):
    if len(b) not in (_BASE_LEN, _BASE_LEN + 4):
        raise ValueError(f"Unexpected size {len(b)} (want 92 or 96).")
    parts = struct.unpack(_BASE_FMT, b[:_BASE_LEN])
    (
        time_ms, car_raw, flags, gear, plid,
        speed, kmh, mph, rpm, turbo, bar, psi, limiter, thr, brk, clt
    ) = parts
    id_val = None
    if len(b) == _BASE_LEN + 4:
        (id_val,) = struct.unpack("<i", b[_BASE_LEN:_BASE_LEN+4])

    car = car_raw[:3].decode("ascii", errors="ignore").rstrip("\x00") or "ERX"
    return {
        "time": int(time_ms),
        "car": car,
        "flags": int(flags),
        "gear": int(gear),
        "plid": int(plid),
        "speed": float(speed),      # m/s
        "kmh": float(kmh),
        "mph": float(mph),
        "rpm": float(rpm),
        "turbo": float(turbo),      # bar (ERX sets OG_BAR)
        "bar": float(bar),
        "psi": float(psi),
        "limiter": float(limiter),
        "throttle": float(thr),
        "brake": float(brk),
        "clutch": float(clt),
        "id": id_val if id_val is not None else 0,
    }


# ------------- UDP listeners (robust) -------------
def json_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((BIND_ADDR_UDP, JSON_PORT))
    print(f"[{now_str()}] JSON listening on {BIND_ADDR_UDP}:{JSON_PORT}")
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            try:
                obj = json.loads(data.decode("utf-8", errors="replace"))
                with latest_lock:
                    global latest
                    latest = obj
            except Exception:
                pass
        except Exception as e:
            print(f"[{now_str()}] JSON socket error: {e}")
            time.sleep(0.1)


def bin_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((BIND_ADDR_UDP, BIN_PORT))
    print(f"[{now_str()}] BIN listening on {BIND_ADDR_UDP}:{BIN_PORT}")
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            try:
                obj = parse_outgauge_packet(data)
                with latest_lock:
                    global latest
                    latest = obj
            except Exception:
                pass
        except Exception as e:
            print(f"[{now_str()}] BIN socket error: {e}")
            time.sleep(0.1)


# ------------- SSE broadcaster (robust) -------------
def sse_broadcaster():
    print(f"[{now_str()}] SSE broadcaster @ {BROADCAST_HZ} Hz")
    period = 1.0 / BROADCAST_HZ
    while True:
        start = time.time()
        with latest_lock:
            payload = latest.copy() if latest is not None else None
        if payload is not None:
            try:
                payload["speed_kmh"] = float(payload.get("kmh", 0.0))
                payload["speed_mph"] = float(payload.get("mph", 0.0))
                payload["rpm"] = float(payload.get("rpm", 0.0))
                payload["turbo"] = float(payload.get("turbo", 0.0))
                payload["psi"] = float(payload.get("psi", 0.0))
                payload["gear"] = int(payload.get("gear", 1))
            except Exception:
                pass
            line = "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n"
            encoded = line.encode("utf-8")
            dead = []
            with clients_lock:
                for w in list(clients):
                    try:
                        w.write(encoded)
                        w.flush()
                    except Exception:
                        dead.append(w)
                for w in dead:
                    clients.discard(w)
        dt = time.time() - start
        time.sleep(max(0.0, period - dt))


# ------------- HTML (round side-by-side gauges) -------------
INDEX_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
<title>ErinsMod OutGauge Dashboard</title>
<style>
:root{--bg:#05080c;--panel:#0d1420;--ring:#182233;--tick:#2a3750;--needle:#7cd6ff;--text:#e6eef8;--muted:#8aa0bf}
*{box-sizing:border-box}
html,body{height:100%;margin:0;background:var(--bg);color:var(--text);font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial}
.header{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;color:var(--muted);font-size:14px}
.brand{color:var(--text);font-weight:700;letter-spacing:.04em}
.grid{display:grid;gap:14px;padding:10px}
@media (orientation:landscape){.grid{grid-template-columns:1fr 1fr 1fr;height:calc(100% - 44px)}}
@media (orientation:portrait){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid #152033;border-radius:16px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:8px;min-height:33vh}
.title{font-size:12px;color:var(--muted);letter-spacing:.12em;text-transform:uppercase;margin:6px 0 8px}
.gauge-wrap{aspect-ratio:1/1;width:100%;max-width:420px;display:flex;align-items:center;justify-content:center}
canvas{width:100%;height:auto;display:block}
.readout{margin-top:6px;color:var(--muted);font-size:13px}
.value{font-variant-numeric:tabular-nums;color:var(--text)}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;background:#0d1420;border:1px solid #1c2a40;color:var(--muted)}
</style>
</head>
<body>
  <div class="header">
    <div class="brand">ErinsMod OutGauge Dashboard</div>
    <div id="status">Waiting for data…</div>
  </div>
  <div class="grid">
    <div class="card">
      <div class="title">Speed</div>
      <div class="gauge-wrap"><canvas id="gSpd" width="480" height="480"></canvas></div>
      <div class="readout"><span class="badge"><span class="value" id="spdVal">0</span> mph</span></div>
    </div>
    <div class="card">
      <div class="title">Tachometer</div>
      <div class="gauge-wrap"><canvas id="gRpm" width="480" height="480"></canvas></div>
      <div class="readout"><span class="badge"><span class="value" id="rpmVal">0</span> rpm</span></div>
    </div>
    <div class="card">
      <div class="title">Boost</div>
      <div class="gauge-wrap"><canvas id="gBoost" width="480" height="480"></canvas></div>
      <div class="readout"><span class="badge"><span class="value" id="boostVal">0.00</span> psi</span></div>
    </div>
  </div>

<script>
function drawRoundGauge(ctx, value, vmin, vmax, opts={}){
  const w=ctx.canvas.width, h=ctx.canvas.height;
  const cx=w/2, cy=h/2, r=Math.min(w,h)*0.42;
  ctx.clearRect(0,0,w,h);

  // base ring
  ctx.lineWidth = r*0.14;
  ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--ring').trim();
  ctx.beginPath(); ctx.arc(cx,cy,r,0,Math.PI*2); ctx.stroke();

  // sweep
  const start = Math.PI*0.75, end = Math.PI*2.25;
  const t = Math.max(0, Math.min(1, (value-vmin)/(vmax-vmin)));
  const ang = start + (end-start)*t;
  ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--needle').trim();
  ctx.beginPath(); ctx.arc(cx,cy,r,start,ang); ctx.stroke();

  // ticks
  const tickColor = getComputedStyle(document.documentElement).getPropertyValue('--tick').trim();
  ctx.strokeStyle = tickColor;
  ctx.lineWidth = r*0.02;
  const major = opts.major || 10;
  const minor = opts.minor || 5;
  const sweep = end - start;
  for(let i=0;i<=major;i++){
    const a = start + sweep*(i/major);
    const o1 = r*0.86, o2 = r*0.72;
    const x1=cx+Math.cos(a)*o1, y1=cy+Math.sin(a)*o1;
    const x2=cx+Math.cos(a)*o2, y2=cy+Math.sin(a)*o2;
    ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();

    if(i<major){
      for(let m=1;m<minor;m++){
        const am = start + sweep*((i+m/minor)/major);
        const mm1 = r*0.84, mm2 = r*0.78;
        const mx1=cx+Math.cos(am)*mm1, my1=cy+Math.sin(am)*mm1;
        const mx2=cx+Math.cos(am)*mm2, my2=cy+Math.sin(am)*mm2;
        ctx.beginPath(); ctx.moveTo(mx1,my1); ctx.lineTo(mx2,my2); ctx.stroke();
      }
    }
  }

  // hub
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--panel').trim();
  ctx.beginPath(); ctx.arc(cx,cy,r*0.08,0,Math.PI*2); ctx.fill();
  ctx.lineWidth = r*0.01;
  ctx.strokeStyle = tickColor;
  ctx.beginPath(); ctx.arc(cx,cy,r*0.08,0,Math.PI*2); ctx.stroke();

  // needle
  const needleColor = getComputedStyle(document.documentElement).getPropertyValue('--needle').trim();
  ctx.save();
  ctx.translate(cx,cy);
  ctx.rotate(ang - Math.PI/2);
  ctx.fillStyle = needleColor;
  const L = r*-0.9, W = r*0.04;
  ctx.beginPath();
  ctx.moveTo(-W, 0);
  ctx.lineTo(W, 0);
  ctx.lineTo(0, -L);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

let latest=null;
const smooth={spd:0,rpm:0,boost:0};
function lerp(a,b,t){return a+(b-a)*t;}

function animate(){
  const now = performance.now();
  const alpha = 1 - Math.exp(-(now-(animate._p||now))/120); // smoother
  animate._p = now;

  const spdMax=200, rpmMax=10000, boostMax=40.0;

  if(latest){
    const spd = Math.max(0, Math.min(spdMax, (latest.speed_mph||0)));
    const rpm = Math.max(0, Math.min(20000, (latest.rpm||0)));
    const boost = Math.max(0, Math.min(40.0, (latest.psi||0)));
    smooth.spd = lerp(smooth.spd, spd, alpha);
    smooth.rpm = lerp(smooth.rpm, rpm, alpha);
    smooth.boost = lerp(smooth.boost, boost, alpha);
  }

  drawRoundGauge(spdCtx, smooth.spd, 0, spdMax, {major:10,minor:5});
  drawRoundGauge(rpmCtx, smooth.rpm, 0, rpmMax, {major:9,minor:5});
  drawRoundGauge(boostCtx, smooth.boost, 0, boostMax, {major:8,minor:5});

  document.getElementById('spdVal').textContent = smooth.spd.toFixed(0);
  document.getElementById('rpmVal').textContent = smooth.rpm.toFixed(0);
  document.getElementById('boostVal').textContent = smooth.boost.toFixed(2);

  requestAnimationFrame(animate);
}

const spdCtx = document.getElementById('gSpd').getContext('2d');
const rpmCtx = document.getElementById('gRpm').getContext('2d');
const boostCtx = document.getElementById('gBoost').getContext('2d');
requestAnimationFrame(animate);

// SSE
const statusEl = document.getElementById('status');
const es = new EventSource('/stream');
es.onmessage = (e)=>{
  latest = JSON.parse(e.data);
  const car = latest.car || "ERX";
  const g = latest.gear ?? 1;
  const gearTxt = (g===0) ? 'R' : (g===1 ? 'N' : (g-1));
  statusEl.textContent = `Car ${car} • Gear ${gearTxt} • ${Math.round(latest.rpm||0)} rpm`;
};
es.onerror = ()=>{ statusEl.textContent = "Disconnected. Retrying…"; };
</script>
</body>
</html>
"""

# ------------- HTTP server (robust) -------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stdout.write("%s - - [%s] %s\n" % (self.client_address[0], now_str(), fmt%args))

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b":ok\n\n")
                self.wfile.flush()
            except Exception:
                return
            with clients_lock:
                clients.add(self.wfile)
            try:
                while True:
                    time.sleep(60)
            except Exception:
                pass
            finally:
                with clients_lock:
                    clients.discard(self.wfile)
            return

        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Not found")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    lan_ip = get_lan_ip_hint()
    print("=== ErinsMod OutGauge Dashboard ===")
    print(f"HTTP   : http://0.0.0.0:{HTTP_PORT}/  (open http://{lan_ip}:{HTTP_PORT}/ on your LAN)")
    print(f"LAN IP : {lan_ip}   {'(looks like your 192.168.1.* address)' if lan_ip.startswith('192.168.1.') else ''}")
    print(f"UDP In : {BIND_ADDR_UDP}:{JSON_PORT} (JSON), {BIND_ADDR_UDP}:{BIN_PORT} (binary)")
    # Start listeners and broadcaster
    t1 = threading.Thread(target=json_listener, daemon=True); t1.start()
    t2 = threading.Thread(target=bin_listener, daemon=True); t2.start()
    t3 = threading.Thread(target=sse_broadcaster, daemon=True); t3.start()

    # HTTP server
    srv = ThreadingHTTPServer((BIND_ADDR_HTTP, HTTP_PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()

