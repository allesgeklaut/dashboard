#!/usr/bin/env python3
"""
HOMELAB//CTRL v2 - Interactive Terminal Dashboard
pip install textual psutil requests
Optional (touchscreen): pip install evdev
  then: sudo usermod -a -G input $USER  (re-login after)
"""
from __future__ import annotations
import time, subprocess, socket, os, threading
from datetime import datetime, timedelta
import psutil, requests, urllib3
from rich.text import Text
from rich.table import Table
from rich.panel import Panel
from rich import box
from textual.app import App, ComposeResult
from textual.widgets import Static, DataTable, Button, Footer
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual import on
import json, sys
from secrets import PORTAINER_API_KEY

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import evdev
    EVDEV = True
except ImportError:
    EVDEV = False

# ── CONFIG ────────────────────────────────────────────────────────
PORTAINER_URL     = "https://192.168.0.43:9443"
PORTAINER_API_KEY = PORTAINER_API_KEY
NFS_MOUNTS        = ["/mnt/nas"]
REFRESH_SECS      = 2
CAL_FILE          = os.path.expanduser("~/.homelab_cal.json")

def load_calibration():
    """Load saved touch calibration, or return None."""
    try:
        with open(CAL_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def normalize_touch(raw_x, raw_y, cal, max_x, max_y):
    """Normalize raw touch coords using calibration data if available."""
    if cal:
        rx = cal.get("max_x", max_x) - cal.get("min_x", 0) or max_x
        ry = cal.get("max_y", max_y) - cal.get("min_y", 0) or max_y
        xf = (raw_x - cal.get("min_x", 0)) / rx
        yf = (raw_y - cal.get("min_y", 0)) / ry
    else:
        xf = raw_x / max_x if max_x else 0
        yf = raw_y / max_y if max_y else 0
    return max(0.0, min(1.0, xf)), max(0.0, min(1.0, yf))

# ─────────────────────────────────────────────────────────────────

# ── Portainer / Docker ────────────────────────────────────────────
_eid = None
def _ph(): return {"X-API-Key": PORTAINER_API_KEY}

def get_eid():
    global _eid
    if _eid: return _eid
    try:
        r = requests.get(f"{PORTAINER_URL}/api/endpoints",
                         headers=_ph(), timeout=2, verify=False)
        if r.ok and r.json(): _eid = r.json()[0]["Id"]
    except: pass
    return _eid

def get_containers():
    eid = get_eid()
    if eid:
        try:
            r = requests.get(
                f"{PORTAINER_URL}/api/endpoints/{eid}/docker/containers/json?all=true",
                headers=_ph(), timeout=3, verify=False)
            if r.ok:
                return [{"id": c["Id"][:12], "name": c["Names"][0].lstrip("/"),
                         "state": c["State"], "status": c["Status"],
                         "image": c["Image"].split("/")[-1]}
                        for c in r.json()], "portainer"
        except: pass
    try:
        out = subprocess.check_output(
            ["docker","ps","-a","--format",
             "{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}"],
            stderr=subprocess.DEVNULL, timeout=3).decode().strip()
        rows = []
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) >= 4:
                rows.append({"id": p[0], "name": p[1], "state": p[2],
                             "status": p[3], "image": (p[4] if len(p)>4 else "").split("/")[-1]})
        return rows, "docker"
    except: return [], "none"

def container_action(cid: str, action: str):
    eid = get_eid()
    if eid:
        try:
            r = requests.post(
                f"{PORTAINER_URL}/api/endpoints/{eid}/docker/containers/{cid}/{action}",
                headers=_ph(), timeout=5, verify=False)
            if r.status_code in (204, 304): return True, f"{action} OK"
            return False, f"HTTP {r.status_code}"
        except Exception as e: return False, str(e)
    try:
        subprocess.check_call(["docker", action, cid],
                              stderr=subprocess.DEVNULL, timeout=10)
        return True, f"{action} OK"
    except Exception as e: return False, str(e)

# ── Stats helpers ─────────────────────────────────────────────────
_np = _nt = None
def net_speed():
    global _np, _nt
    n, now = psutil.net_io_counters(), time.time()
    if _np is None: _np, _nt = n, now; return 0.0, 0.0
    dt = now - _nt or 0.001
    tx, rx = (n.bytes_sent - _np.bytes_sent)/dt, (n.bytes_recv - _np.bytes_recv)/dt
    _np, _nt = n, now; return tx, rx

def fmt_b(v):
    if v>1e6: return f"{v/1e6:.1f}MB/s"
    if v>1e3: return f"{v/1e3:.1f}KB/s"
    return f"{v:.0f}B/s"

def pbar(pct, w=12):
    f = int(pct/100*w)
    t = Text()
    c = "bright_red" if pct>85 else "yellow" if pct>65 else "green"
    t.append("█"*f, style=c); t.append("░"*(w-f), style="grey23")
    return t

def get_temp():
    try:
        s = psutil.sensors_temperatures()
        for k in ("coretemp","cpu_thermal","k10temp","acpitz"):
            if k in s and s[k]: return s[k][0].current
    except: pass
    return None

def get_ip():
    for iface, addrs in psutil.net_if_addrs().items():
        if iface == "lo": continue
        for a in addrs:
            if a.family == 2 and not a.address.startswith("127."): return a.address
    return "n/a"

# ── Widgets ───────────────────────────────────────────────────────
class StatsWidget(Static):
    def on_mount(self): self.set_interval(REFRESH_SECS, self._refresh); self._refresh()
    def _refresh(self):
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory()
        swp = psutil.swap_memory()
        l1, l5, l15 = psutil.getloadavg()
        nc = psutil.cpu_count() or 1
        tx, rx = net_speed()
        tmp = get_temp()
        t = Table.grid(expand=True, padding=(0,1))
        t.add_column(width=7, style="dim green")
        t.add_column(width=14)
        t.add_column(width=18, justify="right")
        def r(label, pct, val):
            vc = "bright_red" if pct>85 else "yellow" if pct>65 else "bright_green"
            t.add_row(label, pbar(pct), Text(val, style=vc))
        r("CPU",  cpu,  f"{cpu:.0f}%")
        r("RAM",  mem.percent, f"{mem.percent:.0f}% {mem.used/1e9:.1f}/{mem.total/1e9:.1f}GB")
        r("SWAP", swp.percent, f"{swp.percent:.0f}%")
        if tmp: r("TEMP", min(tmp,100), f"{tmp:.0f}°C")
        t.add_row("LOAD", Text(""), Text(f"1m {l1:.2f}  5m {l5:.2f}  15m {l15:.2f}", style="green"))
        t.add_row("NET",  Text(""), Text(f"▲{fmt_b(tx)}  ▼{fmt_b(rx)}", style="green"))
        self.update(Panel(t, title=Text("► RESOURCES", style="green"),
                          style="green", box=box.SIMPLE_HEAVY, padding=(0,0)))

class StorageWidget(Static):
    def on_mount(self): self.set_interval(5, self._refresh); self._refresh()
    def _refresh(self):
        t = Table.grid(padding=(0,1))
        t.add_column(width=14, style="dim green")
        t.add_column(width=12)
        t.add_column(width=14, justify="right")
        shown = set()
        for mp in NFS_MOUNTS:
            if not os.path.ismount(mp):
                t.add_row(Text(mp[:12], style="dim"), Text("not mounted"), Text("NFS", style="dim"))
                shown.add(mp); continue
            try:
                u = psutil.disk_usage(mp); pct = u.percent
                t.add_row(Text(mp[:12], style="green"), pbar(pct, 10),
                          Text(f"{u.used/1e9:.0f}/{u.total/1e9:.0f}G NFS",
                               style="bright_red" if pct>85 else "yellow" if pct>65 else "green"))
                shown.add(mp)
            except: t.add_row(Text(mp[:12], style="dim"), Text("err", style="red"), Text(""))
        cnt = 0
        for p in psutil.disk_partitions(all=False):
            if p.mountpoint in shown or cnt >= 3: continue
            if any(x in p.fstype for x in ("tmpfs","devtmp","squash","overlay")): continue
            try:
                u = psutil.disk_usage(p.mountpoint)
                if u.total < 1e8: continue
                pct = u.percent
                t.add_row(Text(p.mountpoint[:12], style="dim green"), pbar(pct, 10),
                          Text(f"{u.used/1e9:.0f}/{u.total/1e9:.0f}G",
                               style="bright_red" if pct>85 else "yellow" if pct>65 else "green"))
                shown.add(p.mountpoint); cnt += 1
            except: continue
        self.update(Panel(t, title=Text("► STORAGE", style="green"),
                          style="green", box=box.SIMPLE_HEAVY, padding=(0,0)))

class NetworkWidget(Static):
    def on_mount(self): self.set_interval(5, self._refresh); self._refresh()
    def _refresh(self):
        td = timedelta(seconds=int(time.time()-psutil.boot_time()))
        h, r2 = divmod(td.seconds, 3600); m = r2 // 60
        t = Table.grid(padding=(0,2))
        t.add_column(width=12, style="dim green")
        t.add_column(style="green")
        t.add_row("IP",       get_ip())
        t.add_row("UPTIME",   f"{td.days}d {h}h {m}m")
        ok = get_eid() is not None
        t.add_row("SOURCE",   Text("PORTAINER" if ok else "DOCKER CLI",
                                   style="bright_green" if ok else "yellow"))
        self.update(Panel(t, title=Text("► NETWORK", style="green"),
                          style="green", box=box.SIMPLE_HEAVY, padding=(0,0)))


def run_calibration():
    """Interactive 4-corner touch calibration. Saves to CAL_FILE."""
    if not EVDEV:
        print("evdev not installed. pip install evdev"); return

    import evdev as ev
    MT_X  = ev.ecodes.ABS_MT_POSITION_X
    MT_Y  = ev.ecodes.ABS_MT_POSITION_Y
    MT_ID = ev.ecodes.ABS_MT_TRACKING_ID
    ABS_X = ev.ecodes.ABS_X
    ABS_Y = ev.ecodes.ABS_Y

    # Find finger/touch device (prefer "Finger" or multitouch)
    devs = [ev.InputDevice(p) for p in ev.list_devices()]
    device = None
    for d in devs:
        caps = d.capabilities()
        if ev.ecodes.EV_ABS not in caps: continue
        codes = [c for c, _ in caps[ev.ecodes.EV_ABS]]
        if MT_X in codes and MT_Y in codes and "Finger" in d.name:
            device = d; break
    if not device:
        for d in devs:
            caps = d.capabilities()
            if ev.ecodes.EV_ABS not in caps: continue
            codes = [c for c, _ in caps[ev.ecodes.EV_ABS]]
            if MT_X in codes and MT_Y in codes:
                device = d; break
    if not device:
        print("No multitouch device found!"); return

    abs_map = dict(device.capabilities()[ev.ecodes.EV_ABS])
    max_x = abs_map[MT_X].max
    max_y = abs_map[MT_Y].max
    print(f"\n  Device : {device.name}")
    print(f"  Range  : X 0-{max_x}  Y 0-{max_y}\n")

    corners = [
        ("TOP-LEFT  "),
        ("TOP-RIGHT "),
        ("BOTTOM-LEFT "),
        ("BOTTOM-RIGHT"),
    ]
    points = []
    for label in corners:
        print(f"  ┌─ Touch the {label} corner and hold ─┐")
        cx = cy = 0
        done = False
        for event in device.read_loop():
            if event.type == ev.ecodes.EV_ABS:
                if event.code == MT_X: cx = event.value
                elif event.code == MT_Y: cy = event.value
                elif event.code == MT_ID and event.value != -1 and not done:
                    print(f"  │  X={cx:6d}  Y={cy:6d}              │")
                    print(f"  └────────────────────────────────────┘\n")
                    points.append((cx, cy))
                    done = True
            if done: break
        import time; time.sleep(0.6)

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    cal = {"min_x": min(xs), "max_x": max(xs),
           "min_y": min(ys), "max_y": max(ys)}
    with open(CAL_FILE, "w") as f:
        json.dump(cal, f, indent=2)
    print(f"  ✓ Calibration saved to {CAL_FILE}")
    print(f"    X: {cal['min_x']} → {cal['max_x']}")
    print(f"    Y: {cal['min_y']} → {cal['max_y']}\n")

# ── App ───────────────────────────────────────────────────────────
class HomelabApp(App):
    CSS = """
    Screen        { background: #0a0a0a; }
    #topbar       { height: 1; background: #001800; color: #00ff00; padding: 0 1; }
    #left         { width: 36; }
    #right        { width: 1fr; }
    #tbl          { height: 1fr; }
    #actionbar    { height: 3; background: #001800; align: center middle; padding: 0 1; }
    #statusbar    { height: 1; background: #001800; color: green; padding: 0 1; }
    Button        { min-width: 14; margin: 0 1; background: #002200;
                    color: #00ff00; border: solid green; }
    Button:focus  { background: #004400; border: solid #00ff00; }
    Button.-stop  { border: solid red; color: #ff4444; }
    DataTable     { background: #0a0a0a; color: green; }
    StatsWidget   { height: auto; }
    StorageWidget { height: auto; }
    NetworkWidget { height: auto; }
    """
    BINDINGS = [
        ("s", "act('start')",   "Start"),
        ("x", "act('stop')",    "Stop"),
        ("t", "act('restart')", "Restart"),
        ("r", "refresh",        "Refresh"),
        ("q", "quit",           "Quit"),
    ]

    _containers: list = []
    selected_id: reactive[str | None] = reactive(None)
    status_msg: reactive[str] = reactive("Use ↑↓ to select a container.")

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Horizontal():
            with Vertical(id="left"):
                yield StatsWidget()
                yield StorageWidget()
                yield NetworkWidget()
            with Vertical(id="right"):
                yield DataTable(id="tbl", cursor_type="row")
                with Horizontal(id="actionbar"):
                    yield Button("▶  START",   id="b-start")
                    yield Button("■  STOP",    id="b-stop",    classes="-stop")
                    yield Button("↺  RESTART", id="b-restart")
                    yield Button("⟳  REFRESH", id="b-refresh")
        yield Static(id="statusbar")

    def on_mount(self):
        tbl = self.query_one("#tbl", DataTable)
        tbl.add_columns(" ", "NAME", "STATE", "STATUS", "IMAGE")
        self._do_refresh()
        self.set_interval(REFRESH_SECS, self._do_refresh)
        self.set_interval(1, self._tick)
        self._cal = load_calibration()
        if EVDEV: threading.Thread(target=self._touch_loop, daemon=True).start()

    def _tick(self):
        now = datetime.now().strftime("%H:%M:%S")
        host = socket.gethostname()
        t = Text()
        t.append(" HOMELAB", style="bold bright_green")
        t.append("//", style="dim green")
        t.append("CTRL", style="bold bright_green")
        t.append(f"   ●  {host}  ●  {now}", style="dim green")
        self.query_one("#topbar").update(t)
        keys = "  │  s:Start  x:Stop  t:Restart  r:Refresh  q:Quit"
        self.query_one("#statusbar").update(
            Text(f" ► {self.status_msg}{keys}", style="dim green"))

    def _do_refresh(self):
        containers, src = get_containers()
        self._containers = containers
        tbl = self.query_one("#tbl", DataTable)
        prev_id = self.selected_id   # remember selection before clear
        tbl.clear()
        restore_row = 0
        for i, c in enumerate(containers):
            up = c["state"] == "running"
            dot  = Text("● " if up else "✖ ", style="bright_green" if up else "bright_red")
            stat = Text(c["state"], style="bright_green" if up else "bright_red")
            tbl.add_row(dot, c["name"], stat, c["status"][:22], c["image"][:26], key=c["id"])
            if c["id"] == prev_id:
                restore_row = i
        # Restore cursor without losing selection
        if tbl.row_count > 0:
            tbl.move_cursor(row=restore_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        self.selected_id = event.row_key.value
        name = next((c["name"] for c in self._containers if c["id"] == self.selected_id), "?")
        self.status_msg = f"Selected: {name}"

    @on(Button.Pressed, "#b-start")   
    def _btn_start(self):   self.action_act("start")
    @on(Button.Pressed, "#b-stop")    
    def _btn_stop(self):    self.action_act("stop")
    @on(Button.Pressed, "#b-restart") 
    def _btn_restart(self): self.action_act("restart")
    @on(Button.Pressed, "#b-refresh") 
    def _btn_refresh(self): self.action_refresh()

    def action_act(self, verb: str):
        if not self.selected_id:
            self.status_msg = "No container selected — use ↑↓ arrows first"; return
        name = next((c["name"] for c in self._containers if c["id"] == self.selected_id), "?")
        self.status_msg = f"Running {verb} on {name}…"
        self._tick()
        ok, msg = container_action(self.selected_id, verb)
        self.status_msg = f"{'✓' if ok else '✗'} {verb} {name}: {msg}"
        self.set_timer(1.2, self._do_refresh)

    def action_refresh(self):
        self._do_refresh(); self.status_msg = "Refreshed."

    # ── Touch screen via evdev ────────────────────────────────────
    def _touch_loop(self):
        try:
            devs = [evdev.InputDevice(p) for p in evdev.list_devices()]
            # Prefer multitouch finger device (ABS_MT_POSITION_X) over pen/stylus
            MT_X = evdev.ecodes.ABS_MT_POSITION_X
            MT_Y = evdev.ecodes.ABS_MT_POSITION_Y
            MT_ID = evdev.ecodes.ABS_MT_TRACKING_ID
            dev = None
            for d in devs:
                caps = d.capabilities()
                if evdev.ecodes.EV_ABS not in caps: continue
                abs_codes = [c for c, _ in caps[evdev.ecodes.EV_ABS]]
                if MT_X in abs_codes and MT_Y in abs_codes:
                    dev = d
                    break  # found multitouch finger device
            if not dev: return
            abs_map = dict(dev.capabilities()[evdev.ecodes.EV_ABS])
            max_x = abs_map[MT_X].max
            max_y = abs_map[MT_Y].max
            cx = cy = 0
            for ev in dev.read_loop():
                if ev.type == evdev.ecodes.EV_ABS:
                    if ev.code == MT_X: cx = ev.value
                    elif ev.code == MT_Y: cy = ev.value
                    elif ev.code == MT_ID and ev.value != -1:
                        # New finger touch down — fire action
                        if max_x > 0 and max_y > 0:
                            xf, yf = normalize_touch(cx, cy, self._cal, max_x, max_y)
                            self.call_from_thread(self._on_touch, xf, yf)
        except Exception: pass

    def _on_touch(self, xf: float, yf: float):
        """Map normalised touch coords to UI actions using actual widget regions."""
        try:
            sz = self.size          # terminal dimensions in characters
            if sz.width == 0 or sz.height == 0:
                return
            # Convert touch fraction → character cell position
            cx = xf * sz.width
            cy = yf * sz.height

            # ── Action bar buttons (query each button's real region) ──
            try:
                for btn_id, verb in [("b-start",   "start"),
                                     ("b-stop",    "stop"),
                                     ("b-restart", "restart"),
                                     ("b-refresh", None)]:
                    btn = self.query_one(f"#{btn_id}")
                    r   = btn.region
                    if r.x <= cx <= r.x + r.width and r.y <= cy <= r.y + r.height:
                        if verb: self.action_act(verb)
                        else:    self.action_refresh()
                        return
            except Exception:
                pass

            # ── Container table rows ───────────────────────────────
            try:
                tbl_w = self.query_one("#tbl")
                r = tbl_w.region
                if r.x <= cx <= r.x + r.width and r.y <= cy <= r.y + r.height:
                    # subtract 1 header row + 1 border row
                    row_idx = max(0, int(cy - r.y - 2))
                    if row_idx < len(self._containers):
                        c = self._containers[row_idx]
                        self.selected_id = c["id"]
                        tbl = self.query_one("#tbl", DataTable)
                        tbl.move_cursor(row=row_idx)
                        self.status_msg = f"Touch → {c['name']}"
            except Exception:
                pass
        except Exception:
            pass

if __name__ == "__main__":
    if "--calibrate" in sys.argv:
        run_calibration()
    else:
        psutil.cpu_percent(interval=0.1)
        net_speed()
        HomelabApp().run()
