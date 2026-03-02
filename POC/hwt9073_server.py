"""
HWT9073-485 IMU Dashboard Server
Run: jy901_env/bin/python3 hwt9073_server.py
Open: http://localhost:5000
"""

import serial, struct, time, threading, json
from flask import Flask, Response

PORT = "/dev/cu.usbserial-310"
BAUD = 9600
ADDR = 0x50

app = Flask(__name__)
imu_data = {"roll": 0, "pitch": 0, "yaw": 0,
            "ax": 0, "ay": 0, "az": 0,
            "gx": 0, "gy": 0, "gz": 0, "temp": 0}

# ── Modbus helpers ──────────────────────────────────────
def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def read_regs(ser, addr, reg, count):
    msg = struct.pack('>BBHH', addr, 0x03, reg, count)
    msg += struct.pack('<H', crc16(msg))
    ser.reset_input_buffer()
    ser.write(msg)
    time.sleep(0.05)
    resp = ser.read(3 + count * 2 + 2)
    if len(resp) < 3 + count * 2 + 2: return None
    if crc16(resp[:-2]) != struct.unpack('<H', resp[-2:])[0]: return None
    return [struct.unpack('>h', resp[3+i*2:5+i*2])[0] for i in range(count)]

# ── Background IMU reader ───────────────────────────────
def imu_loop():
    global imu_data
    while True:
        try:
            ser = serial.Serial(PORT, BAUD, timeout=0.5)
            while True:
                a = read_regs(ser, ADDR, 0x34, 3)
                g = read_regs(ser, ADDR, 0x37, 3)
                n = read_regs(ser, ADDR, 0x3D, 3)
                t = read_regs(ser, ADDR, 0x40, 1)
                if a: imu_data.update(ax=a[0]/32768*16, ay=a[1]/32768*16, az=a[2]/32768*16)
                if g: imu_data.update(gx=g[0]/32768*2000, gy=g[1]/32768*2000, gz=g[2]/32768*2000)
                if n: imu_data.update(roll=n[0]/32768*180, pitch=n[1]/32768*180, yaw=n[2]/32768*180)
                if t: imu_data["temp"] = t[0]/100
                time.sleep(0.05)
        except Exception as e:
            print(f"IMU error: {e} — retrying in 2s")
            time.sleep(2)

# ── SSE stream ──────────────────────────────────────────
@app.route("/stream")
def stream():
    def gen():
        while True:
            yield f"data: {json.dumps(imu_data)}\n\n"
            time.sleep(0.05)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── HTML dashboard ──────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>HWT9073 IMU Dashboard</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0d1117; color:#e6edf3; font-family:'Courier New',monospace;
         display:flex; flex-direction:column; align-items:center; min-height:100vh; padding:24px; }
  h1  { font-size:1.1rem; letter-spacing:4px; color:#58a6ff; margin-bottom:24px; text-transform:uppercase; }
  .grid { display:grid; grid-template-columns:260px 260px 260px; gap:16px; }

  .card { background:#161b22; border:1px solid #30363d; border-radius:12px; padding:18px; }
  .card h2 { font-size:.65rem; letter-spacing:3px; color:#8b949e; margin-bottom:14px; text-transform:uppercase; }

  /* Attitude indicator */
  #ahi { display:block; border-radius:50%; margin:0 auto; }

  /* Compass */
  #compass { display:block; margin:0 auto; }

  /* Value rows */
  .val-row { display:flex; justify-content:space-between; align-items:center;
             padding:6px 0; border-bottom:1px solid #21262d; }
  .val-row:last-child { border:none; }
  .val-label { color:#8b949e; font-size:.8rem; }
  .val-num   { font-size:.95rem; font-weight:bold; min-width:80px; text-align:right; }
  .pos { color:#3fb950; }
  .neg { color:#f85149; }
  .neu { color:#e6edf3; }

  /* Bar */
  .bar-wrap { display:flex; align-items:center; gap:8px; margin:8px 0; }
  .bar-label { width:28px; font-size:.7rem; color:#8b949e; }
  .bar-track { flex:1; height:8px; background:#21262d; border-radius:4px; overflow:hidden; position:relative; }
  .bar-fill  { height:100%; border-radius:4px; transition:width .1s, background .1s; }
  .bar-val   { width:70px; font-size:.75rem; text-align:right; }

  /* Temp */
  .temp-big { font-size:2.5rem; font-weight:bold; color:#58a6ff; text-align:center; margin-top:8px; }
  .status   { font-size:.7rem; color:#3fb950; text-align:center; margin-top:6px; letter-spacing:2px; }

  /* Wide card */
  .wide { grid-column: span 3; }
  .badge { display:inline-block; padding:2px 10px; border-radius:20px;
           background:#1f6feb33; color:#58a6ff; font-size:.65rem; letter-spacing:2px; margin-left:8px; }
</style>
</head>
<body>
<h1>HWT9073-485 &nbsp;<span class="badge">LIVE</span></h1>

<div class="grid">

  <!-- Attitude Indicator -->
  <div class="card">
    <h2>Attitude</h2>
    <canvas id="ahi" width="220" height="220"></canvas>
  </div>

  <!-- Compass -->
  <div class="card">
    <h2>Heading (Yaw)</h2>
    <canvas id="compass" width="220" height="220"></canvas>
    <div style="text-align:center;margin-top:10px;font-size:1.4rem;font-weight:bold" id="yaw-val">0.00°</div>
  </div>

  <!-- Angles -->
  <div class="card">
    <h2>Euler Angles</h2>
    <div class="val-row"><span class="val-label">Roll</span>   <span class="val-num" id="roll">0.00°</span></div>
    <div class="val-row"><span class="val-label">Pitch</span>  <span class="val-num" id="pitch">0.00°</span></div>
    <div class="val-row"><span class="val-label">Yaw</span>    <span class="val-num" id="yaw">0.00°</span></div>
    <br>
    <h2>Temperature</h2>
    <div class="temp-big" id="temp">--</div>
    <div class="status" id="status">CONNECTING...</div>
  </div>

  <!-- Acceleration -->
  <div class="card">
    <h2>Acceleration (g)</h2>
    <div class="bar-wrap"><span class="bar-label">X</span>
      <div class="bar-track"><div class="bar-fill" id="ax-bar"></div></div>
      <span class="bar-val" id="ax-val">0.000</span></div>
    <div class="bar-wrap"><span class="bar-label">Y</span>
      <div class="bar-track"><div class="bar-fill" id="ay-bar"></div></div>
      <span class="bar-val" id="ay-val">0.000</span></div>
    <div class="bar-wrap"><span class="bar-label">Z</span>
      <div class="bar-track"><div class="bar-fill" id="az-bar"></div></div>
      <span class="bar-val" id="az-val">0.000</span></div>
  </div>

  <!-- Gyroscope -->
  <div class="card">
    <h2>Gyroscope (°/s)</h2>
    <div class="bar-wrap"><span class="bar-label">X</span>
      <div class="bar-track"><div class="bar-fill" id="gx-bar"></div></div>
      <span class="bar-val" id="gx-val">0.00</span></div>
    <div class="bar-wrap"><span class="bar-label">Y</span>
      <div class="bar-track"><div class="bar-fill" id="gy-bar"></div></div>
      <span class="bar-val" id="gy-val">0.00</span></div>
    <div class="bar-wrap"><span class="bar-label">Z</span>
      <div class="bar-track"><div class="bar-fill" id="gz-bar"></div></div>
      <span class="bar-val" id="gz-val">0.00</span></div>
  </div>

  <!-- Raw values -->
  <div class="card">
    <h2>Raw Summary</h2>
    <div class="val-row"><span class="val-label">Ax</span><span class="val-num" id="r-ax">0.000 g</span></div>
    <div class="val-row"><span class="val-label">Ay</span><span class="val-num" id="r-ay">0.000 g</span></div>
    <div class="val-row"><span class="val-label">Az</span><span class="val-num" id="r-az">0.000 g</span></div>
    <div class="val-row"><span class="val-label">Gx</span><span class="val-num" id="r-gx">0.00 °/s</span></div>
    <div class="val-row"><span class="val-label">Gy</span><span class="val-num" id="r-gy">0.00 °/s</span></div>
    <div class="val-row"><span class="val-label">Gz</span><span class="val-num" id="r-gz">0.00 °/s</span></div>
  </div>

</div>

<script>
// ── Canvas: Artificial Horizon ──────────────────────────
const ahiCanvas = document.getElementById('ahi');
const ahiCtx    = ahiCanvas.getContext('2d');
const AHI_W = 220, AHI_H = 220, AHI_R = 105;

function drawAHI(roll, pitch) {
  const cx = AHI_W/2, cy = AHI_H/2;
  const r  = roll  * Math.PI / 180;
  const p  = pitch * Math.PI / 180;
  const horizon_shift = p * AHI_R * 1.5;

  ahiCtx.clearRect(0,0,AHI_W,AHI_H);
  ahiCtx.save();

  // Clip to circle
  ahiCtx.beginPath();
  ahiCtx.arc(cx, cy, AHI_R, 0, Math.PI*2);
  ahiCtx.clip();

  // Sky
  ahiCtx.save();
  ahiCtx.translate(cx, cy);
  ahiCtx.rotate(-r);
  ahiCtx.fillStyle = '#0d3b6e';
  ahiCtx.fillRect(-AHI_W, -AHI_H - horizon_shift, AHI_W*2, AHI_H*2);

  // Ground
  ahiCtx.fillStyle = '#5c3d0a';
  ahiCtx.fillRect(-AHI_W, -horizon_shift, AHI_W*2, AHI_H*2);

  // Horizon line
  ahiCtx.strokeStyle = '#ffffff';
  ahiCtx.lineWidth = 2;
  ahiCtx.beginPath();
  ahiCtx.moveTo(-AHI_W, -horizon_shift);
  ahiCtx.lineTo( AHI_W, -horizon_shift);
  ahiCtx.stroke();

  // Pitch lines
  ahiCtx.strokeStyle = 'rgba(255,255,255,0.5)';
  ahiCtx.lineWidth = 1;
  for (let deg of [-20,-10,10,20]) {
    const y = -horizon_shift - deg * (AHI_R * 1.5 / 90);
    const w = deg % 20 === 0 ? 40 : 25;
    ahiCtx.beginPath();
    ahiCtx.moveTo(-w, y); ahiCtx.lineTo(w, y);
    ahiCtx.stroke();
    ahiCtx.fillStyle='rgba(255,255,255,0.7)';
    ahiCtx.font='10px Courier New';
    ahiCtx.fillText(Math.abs(deg), w+4, y+4);
  }
  ahiCtx.restore();

  // Fixed aircraft symbol
  ahiCtx.strokeStyle = '#f0c040';
  ahiCtx.lineWidth = 3;
  ahiCtx.beginPath();
  ahiCtx.moveTo(cx-40, cy); ahiCtx.lineTo(cx-15, cy);
  ahiCtx.moveTo(cx-15, cy); ahiCtx.lineTo(cx, cy+8);
  ahiCtx.moveTo(cx, cy+8);  ahiCtx.lineTo(cx+15, cy);
  ahiCtx.moveTo(cx+15, cy); ahiCtx.lineTo(cx+40, cy);
  ahiCtx.moveTo(cx-5, cy);  ahiCtx.lineTo(cx+5, cy);
  ahiCtx.stroke();

  // Roll arc & tick
  ahiCtx.strokeStyle = '#8b949e';
  ahiCtx.lineWidth = 1;
  ahiCtx.beginPath();
  ahiCtx.arc(cx, cy, AHI_R - 8, -Math.PI*0.85, -Math.PI*0.15);
  ahiCtx.stroke();

  // Roll pointer
  ahiCtx.save();
  ahiCtx.translate(cx, cy);
  ahiCtx.rotate(-r);
  ahiCtx.strokeStyle='#f0c040'; ahiCtx.lineWidth=2;
  ahiCtx.beginPath(); ahiCtx.moveTo(0, -(AHI_R-8)); ahiCtx.lineTo(-5, -(AHI_R-16)); ahiCtx.lineTo(5, -(AHI_R-16)); ahiCtx.closePath();
  ahiCtx.stroke();
  ahiCtx.restore();

  // Border
  ahiCtx.restore();
  ahiCtx.strokeStyle='#30363d'; ahiCtx.lineWidth=3;
  ahiCtx.beginPath(); ahiCtx.arc(cx,cy,AHI_R,0,Math.PI*2); ahiCtx.stroke();
}

// ── Canvas: Compass ─────────────────────────────────────
const cmpCanvas = document.getElementById('compass');
const cmpCtx    = cmpCanvas.getContext('2d');
const CW = 220, CH = 220, CR = 100;

function drawCompass(yaw) {
  const cx = CW/2, cy = CH/2;
  cmpCtx.clearRect(0,0,CW,CH);

  // Background
  cmpCtx.beginPath();
  cmpCtx.arc(cx,cy,CR,0,Math.PI*2);
  cmpCtx.fillStyle='#161b22'; cmpCtx.fill();
  cmpCtx.strokeStyle='#30363d'; cmpCtx.lineWidth=2; cmpCtx.stroke();

  // Ticks & labels
  const dirs = ['N','NE','E','SE','S','SW','W','NW'];
  for (let i=0; i<36; i++) {
    const a = (i*10 - yaw) * Math.PI/180;
    const major = i%9===0, minor5 = i%5===0;
    const r0 = major? CR-18 : minor5? CR-10 : CR-6;
    cmpCtx.save(); cmpCtx.translate(cx,cy); cmpCtx.rotate(a);
    cmpCtx.strokeStyle = major?'#e6edf3':'#8b949e';
    cmpCtx.lineWidth = major?2:1;
    cmpCtx.beginPath(); cmpCtx.moveTo(0,-CR+2); cmpCtx.lineTo(0,-r0); cmpCtx.stroke();
    if (major) {
      const label = dirs[i/9];
      cmpCtx.fillStyle = label==='N'?'#f85149':'#e6edf3';
      cmpCtx.font = `bold 12px Courier New`;
      cmpCtx.textAlign='center'; cmpCtx.textBaseline='middle';
      cmpCtx.fillText(label, 0, -(CR-28));
    }
    cmpCtx.restore();
  }

  // Needle
  cmpCtx.save(); cmpCtx.translate(cx,cy);
  cmpCtx.strokeStyle='#f85149'; cmpCtx.lineWidth=3;
  cmpCtx.beginPath(); cmpCtx.moveTo(0,0); cmpCtx.lineTo(0,-(CR-35)); cmpCtx.stroke();
  cmpCtx.strokeStyle='#58a6ff'; cmpCtx.lineWidth=3;
  cmpCtx.beginPath(); cmpCtx.moveTo(0,0); cmpCtx.lineTo(0, CR-35); cmpCtx.stroke();
  cmpCtx.beginPath(); cmpCtx.arc(0,0,5,0,Math.PI*2);
  cmpCtx.fillStyle='#e6edf3'; cmpCtx.fill();
  cmpCtx.restore();
}

// ── Helpers ─────────────────────────────────────────────
function colorNum(v, decimals=2, unit='') {
  const cls = Math.abs(v)<0.01?'neu': v>0?'pos':'neg';
  return `<span class="${cls}">${v>=0?'+':''}${v.toFixed(decimals)}${unit}</span>`;
}

function setBar(id, value, max) {
  const pct = Math.min(Math.abs(value)/max*100, 100);
  const color = value > 0 ? '#3fb950' : '#f85149';
  document.getElementById(id+'-bar').style.width  = pct+'%';
  document.getElementById(id+'-bar').style.background = color;
  document.getElementById(id+'-val').innerHTML = colorNum(value, id.startsWith('g')?1:3);
}

// ── SSE ─────────────────────────────────────────────────
const src = new EventSource('/stream');
src.onmessage = e => {
  const d = JSON.parse(e.data);

  drawAHI(d.roll, d.pitch);
  drawCompass(d.yaw);

  document.getElementById('roll').innerHTML  = colorNum(d.roll,  2, '°');
  document.getElementById('pitch').innerHTML = colorNum(d.pitch, 2, '°');
  document.getElementById('yaw').innerHTML   = colorNum(d.yaw,   2, '°');
  document.getElementById('yaw-val').innerHTML = colorNum(d.yaw, 2, '°');
  document.getElementById('temp').textContent  = d.temp.toFixed(1) + ' °C';
  document.getElementById('status').textContent = '● LIVE';
  document.getElementById('status').style.color = '#3fb950';

  setBar('ax', d.ax, 2);  setBar('ay', d.ay, 2);  setBar('az', d.az, 2);
  setBar('gx', d.gx, 500); setBar('gy', d.gy, 500); setBar('gz', d.gz, 500);

  document.getElementById('r-ax').innerHTML = colorNum(d.ax, 3, ' g');
  document.getElementById('r-ay').innerHTML = colorNum(d.ay, 3, ' g');
  document.getElementById('r-az').innerHTML = colorNum(d.az, 3, ' g');
  document.getElementById('r-gx').innerHTML = colorNum(d.gx, 2, ' °/s');
  document.getElementById('r-gy').innerHTML = colorNum(d.gy, 2, ' °/s');
  document.getElementById('r-gz').innerHTML = colorNum(d.gz, 2, ' °/s');
};
src.onerror = () => {
  document.getElementById('status').textContent = '● DISCONNECTED';
  document.getElementById('status').style.color = '#f85149';
};

drawAHI(0,0);
drawCompass(0);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return HTML

# ── Start ───────────────────────────────────────────────
if __name__ == "__main__":
    t = threading.Thread(target=imu_loop, daemon=True)
    t.start()
    print("Dashboard → http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, threaded=True)
