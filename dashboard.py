#!/usr/bin/env python3
"""
HOMELAB//CTRL v2 - Interactive Terminal Dashboard
pip install textual psutil requests
Optional (touchscreen): pip install evdev
  then: sudo usermod -a -G input $USER  (re-login after)
"""
from __future__ import annotations
import time, subprocess, socket, os, threading, logging
from datetime import datetime, timedelta
import psutil, requests, urllib3
from rich.text import Text
from rich.table import Table
from rich.panel import Panel
from rich import box
from textual.app import App, ComposeResult
from textual.widgets import Static, DataTable, Button
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual import on
import json, argparse
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
SCREEN_TIMEOUT    = 60   # seconds until screen turns off (0 = disabled)
BACKLIGHT_PATH    = "/sys/class/backlight/intel_backlight"
CAL_FILE          = os.path.expanduser("~/.homelab_cal.json")

log = logging.getLogger("homelab")
LOG_FILE = os.path.expanduser("~/.homelab_dashboard.log")

# ── Calibration helpers ───────────────────────────────────────────

def load_calibration() -> dict | None:
    try:
        with open(CAL_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def normalize_touch(
    raw_x: int, raw_y: int, cal: dict | None, max_x: int, max_y: int
) -> tuple[float, float]:
    if cal:
        span_x = cal.get("max_x", max_x) - cal.get("min_x", 0) or max_x
        span_y = cal.get("max_y", max_y) - cal.get("min_y", 0) or max_y
        xf = (raw_x - cal.get("min_x", 0)) / span_x
        yf = (raw_y - cal.get("min_y", 0)) / span_y
    else:
        xf = raw_x / max_x if max_x else 0.0
        yf = raw_y / max_y if max_y else 0.0
    return max(0.0, min(1.0, xf)), max(0.0, min(1.0, yf))

# ── Portainer / Docker ────────────────────────────────────────────

_HEADERS = {"X-API-Key": PORTAINER_API_KEY}
_eid: int | None = None
_eid_lock = threading.Lock()

def get_eid() -> int | None:
    global _eid
    with _eid_lock:
        if _eid is not None:
            return _eid
    try:
        r = requests.get(
            f"{PORTAINER_URL}/api/endpoints",
            headers=_HEADERS, timeout=2, verify=False,
        )
        if r.ok and r.json():
            with _eid_lock:
                _eid = r.json()[0]["Id"]
            return _eid
    except Exception as exc:
        log.debug("get_eid failed: %s", exc)
    return None

def get_containers() -> tuple[list[dict], str]:
    eid = get_eid()
    if eid is not None:
        try:
            r = requests.get(
                f"{PORTAINER_URL}/api/endpoints/{eid}/docker/containers/json?all=true",
                headers=_HEADERS, timeout=3, verify=False,
            )
            if r.ok:
                return (
                    [
                        {
                            "id":     c["Id"][:12],
                            "name":   c["Names"][0].lstrip("/"),
                            "state":  c["State"],
                            "status": c["Status"],
                            "image":  c["Image"].split("/")[-1],
                        }
                        for c in r.json()
                    ],
                    "portainer",
                )
        except Exception as exc:
            log.debug("get_containers via portainer failed: %s", exc)

    # Fallback: local docker CLI
    try:
        out = subprocess.check_output(
            [
                "docker", "ps", "-a", "--format",
                "{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}",
            ],
            stderr=subprocess.DEVNULL, timeout=3,
        ).decode().strip()
        rows = []
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) >= 4:
                rows.append({
                    "id":     p[0],
                    "name":   p[1],
                    "state":  p[2],
                    "status": p[3],
                    "image":  (p[4] if len(p) > 4 else "").split("/")[-1],
                })
        return rows, "docker"
    except Exception as exc:
        log.debug("get_containers via docker cli failed: %s", exc)

    return [], "none"

def container_action(cid: str, action: str) -> tuple[bool, str]:
    eid = get_eid()
    if eid is not None:
        try:
            r = requests.post(
                f"{PORTAINER_URL}/api/endpoints/{eid}/docker/containers/{cid}/{action}",
                headers=_HEADERS, timeout=15, verify=False,
            )
            if r.status_code in (204, 304):
                return True, f"{action} OK"
            return False, f"HTTP {r.status_code}"
        except Exception as exc:
            return False, str(exc)

    try:
        subprocess.check_call(
            ["docker", action, cid],
            stderr=subprocess.DEVNULL, timeout=10,
        )
        return True, f"{action} OK"
    except Exception as exc:
        return False, str(exc)

# ── Stats helpers ─────────────────────────────────────────────────

_prev_net: psutil._common.snetio | None = None
_prev_net_t: float | None = None

def net_speed() -> tuple[float, float]:
    global _prev_net, _prev_net_t
    n, now = psutil.net_io_counters(), time.monotonic()
    if _prev_net is None:
        _prev_net, _prev_net_t = n, now
        return 0.0, 0.0
    dt = (now - _prev_net_t) or 0.001
    tx = (n.bytes_sent - _prev_net.bytes_sent) / dt
    rx = (n.bytes_recv - _prev_net.bytes_recv) / dt
    _prev_net, _prev_net_t = n, now
    return tx, rx

def fmt_bytes(v: float) -> str:
    if v > 1e6:
        return f"{v/1e6:.1f}MB/s"
    if v > 1e3:
        return f"{v/1e3:.1f}KB/s"
    return f"{v:.0f}B/s"

def pbar(pct: float, w: int = 12) -> Text:
    filled = int(pct / 100 * w)
    colour = "bright_red" if pct > 85 else "yellow" if pct > 65 else "green"
    t = Text()
    t.append("█" * filled, style=colour)
    t.append("░" * (w - filled), style="grey23")
    return t

def get_temp() -> float | None:
    try:
        sensors = psutil.sensors_temperatures()
        for key in ("coretemp", "cpu_thermal", "k10temp", "acpitz"):
            if key in sensors and sensors[key]:
                return sensors[key][0].current
    except Exception:
        pass
    return None

def get_ip() -> str:
    for iface, addrs in psutil.net_if_addrs().items():
        if iface == "lo":
            continue
        for a in addrs:
            if a.family == 2 and not a.address.startswith("127."):
                return a.address
    return "n/a"

# ── Backlight ─────────────────────────────────────────────────────

def _find_backlight() -> str | None:
    import glob as _glob
    for path in [BACKLIGHT_PATH] + _glob.glob("/sys/class/backlight/*"):
        if os.path.exists(f"{path}/brightness"):
            return path
    return None

def _write_brightness(path: str, value: int) -> bool:
    """Write brightness value; returns True on success."""
    # Try direct write first (works when user is in the 'video' group)
    try:
        with open(f"{path}/brightness", "w") as f:
            f.write(str(value))
        return True
    except PermissionError:
        pass
    # Fallback: shell redirect (may work with udev rules / sudo)
    try:
        subprocess.run(
            ["bash", "-c", f"echo {value} > {path}/brightness"],
            check=True, capture_output=True, timeout=2,
        )
        return True
    except Exception as exc:
        log.warning("brightness write failed: %s", exc)
    return False

BRIGHTNESS_OFF = 1   # >0 keeps the touch digitizer alive

def screen_off() -> None:
    path = _find_backlight()
    if path:
        _write_brightness(path, BRIGHTNESS_OFF)

def screen_on() -> None:
    path = _find_backlight()
    if not path:
        return
    try:
        max_b = int(open(f"{path}/max_brightness").read().strip())
        _write_brightness(path, int(max_b * 0.8))
    except Exception as exc:
        log.warning("screen_on failed: %s", exc)

# ── Widgets ───────────────────────────────────────────────────────

class StatsWidget(Static):
    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(REFRESH_SECS, self._refresh)

    def _refresh(self) -> None:
        cpu  = psutil.cpu_percent()
        mem  = psutil.virtual_memory()
        swp  = psutil.swap_memory()
        l1, l5, l15 = psutil.getloadavg()
        tx, rx = net_speed()
        tmp  = get_temp()

        t = Table.grid(expand=True, padding=(0, 1))
        t.add_column(width=7, style="dim green")
        t.add_column(width=14)
        t.add_column(width=18, justify="right")

        def row(label: str, pct: float, val: str) -> None:
            colour = "bright_red" if pct > 85 else "yellow" if pct > 65 else "bright_green"
            t.add_row(label, pbar(pct), Text(val, style=colour))

        row("CPU",  cpu,         f"{cpu:.0f}%")
        row("RAM",  mem.percent, f"{mem.percent:.0f}% {mem.used/1e9:.1f}/{mem.total/1e9:.1f}GB")
        row("SWAP", swp.percent, f"{swp.percent:.0f}%")
        if tmp:
            row("TEMP", min(tmp, 100), f"{tmp:.0f}°C")
        t.add_row("LOAD", Text(""), Text(f"1m {l1:.2f}  5m {l5:.2f}  15m {l15:.2f}", style="green"))
        t.add_row("NET",  Text(""), Text(f"▲{fmt_bytes(tx)}  ▼{fmt_bytes(rx)}", style="green"))

        self.update(Panel(
            t,
            title=Text("► RESOURCES", style="green"),
            style="green", box=box.SIMPLE_HEAVY, padding=(0, 0),
        ))


class StorageWidget(Static):
    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(5, self._refresh)

    def _refresh(self) -> None:
        t = Table.grid(padding=(0, 1))
        t.add_column(width=14, style="dim green")
        t.add_column(width=12)
        t.add_column(width=14, justify="right")
        shown: set[str] = set()

        for mp in NFS_MOUNTS:
            if not os.path.ismount(mp):
                t.add_row(Text(mp[:12], style="dim"), Text("not mounted"), Text("NFS", style="dim"))
                shown.add(mp)
                continue
            try:
                u = psutil.disk_usage(mp)
                pct = u.percent
                t.add_row(
                    Text(mp[:12], style="green"),
                    pbar(pct, 10),
                    Text(
                        f"{u.used/1e9:.0f}/{u.total/1e9:.0f}G NFS",
                        style="bright_red" if pct > 85 else "yellow" if pct > 65 else "green",
                    ),
                )
                shown.add(mp)
            except Exception:
                t.add_row(Text(mp[:12], style="dim"), Text("err", style="red"), Text(""))

        cnt = 0
        for p in psutil.disk_partitions(all=False):
            if p.mountpoint in shown or cnt >= 3:
                continue
            if any(x in p.fstype for x in ("tmpfs", "devtmp", "squash", "overlay")):
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
                if u.total < 1e8:
                    continue
                pct = u.percent
                t.add_row(
                    Text(p.mountpoint[:12], style="dim green"),
                    pbar(pct, 10),
                    Text(
                        f"{u.used/1e9:.0f}/{u.total/1e9:.0f}G",
                        style="bright_red" if pct > 85 else "yellow" if pct > 65 else "green",
                    ),
                )
                shown.add(p.mountpoint)
                cnt += 1
            except Exception:
                continue

        self.update(Panel(
            t,
            title=Text("► STORAGE", style="green"),
            style="green", box=box.SIMPLE_HEAVY, padding=(0, 0),
        ))


class NetworkWidget(Static):
    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(5, self._refresh)

    def _refresh(self) -> None:
        td  = timedelta(seconds=int(time.time() - psutil.boot_time()))
        h, r = divmod(td.seconds, 3600)
        m   = r // 60

        t = Table.grid(padding=(0, 2))
        t.add_column(width=12, style="dim green")
        t.add_column(style="green")
        t.add_row("IP",     get_ip())
        t.add_row("UPTIME", f"{td.days}d {h}h {m}m")
        ok = get_eid() is not None
        t.add_row(
            "SOURCE",
            Text("PORTAINER" if ok else "DOCKER CLI",
                 style="bright_green" if ok else "yellow"),
        )

        self.update(Panel(
            t,
            title=Text("► NETWORK", style="green"),
            style="green", box=box.SIMPLE_HEAVY, padding=(0, 0),
        ))


# ── Calibration ───────────────────────────────────────────────────

def run_calibration() -> None:
    if not EVDEV:
        print("evdev not installed. pip install evdev")
        return

    MT_X  = evdev.ecodes.ABS_MT_POSITION_X
    MT_Y  = evdev.ecodes.ABS_MT_POSITION_Y
    MT_ID = evdev.ecodes.ABS_MT_TRACKING_ID

    devs = [evdev.InputDevice(p) for p in evdev.list_devices()]
    device = None
    for d in devs:
        caps = d.capabilities()
        if evdev.ecodes.EV_ABS not in caps:
            continue
        codes = [c for c, _ in caps[evdev.ecodes.EV_ABS]]
        if MT_X in codes and MT_Y in codes and "Finger" in d.name:
            device = d
            break
    if not device:
        for d in devs:
            caps = d.capabilities()
            if evdev.ecodes.EV_ABS not in caps:
                continue
            codes = [c for c, _ in caps[evdev.ecodes.EV_ABS]]
            if MT_X in codes and MT_Y in codes:
                device = d
                break
    if not device:
        print("No multitouch device found!")
        return

    abs_map = dict(device.capabilities()[evdev.ecodes.EV_ABS])
    max_x   = abs_map[MT_X].max
    max_y   = abs_map[MT_Y].max
    print(f"\n  Device : {device.name}")
    print(f"  Range  : X 0-{max_x}  Y 0-{max_y}\n")

    corners = ["TOP-LEFT  ", "TOP-RIGHT ", "BOTTOM-LEFT ", "BOTTOM-RIGHT"]
    points: list[tuple[int, int]] = []

    for label in corners:
        print(f"  ┌─ Touch the {label} corner and hold ─┐")
        cx = cy = 0
        done = False
        for event in device.read_loop():
            if event.type == evdev.ecodes.EV_ABS:
                if event.code == MT_X:
                    cx = event.value
                elif event.code == MT_Y:
                    cy = event.value
                elif event.code == MT_ID and event.value != -1 and not done:
                    print(f"  │  X={cx:6d}  Y={cy:6d}              │")
                    print(f"  └────────────────────────────────────┘\n")
                    points.append((cx, cy))
                    done = True
            if done:
                break
        time.sleep(0.6)

    xs  = [p[0] for p in points]
    ys  = [p[1] for p in points]
    cal = {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}
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
    Button.-stop    { border: solid red; color: #ff4444; }
    Button.-screen  { border: solid #555; color: #888888; }
    DataTable     { background: #0a0a0a; color: green; }
    StatsWidget   { height: auto; }
    StorageWidget { height: auto; }
    NetworkWidget { height: auto; }
    """
    BINDINGS = []

    _containers: list[dict] = []
    selected_id: reactive[str | None] = reactive(None)
    status_msg:  reactive[str]        = reactive("Use ↑↓ to select a container.")

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
                    yield Button("■  STOP",    id="b-stop",   classes="-stop")
                    yield Button("↺  RESTART", id="b-restart")
                    yield Button("⟳  REFRESH", id="b-refresh")
                    yield Button("⏻  SCREEN",  id="b-screen", classes="-screen")
        yield Static(id="statusbar")

    def on_mount(self) -> None:
        tbl = self.query_one("#tbl", DataTable)
        tbl.add_columns(" ", "NAME", "STATE", "STATUS", "IMAGE")
        self._do_refresh()
        self.set_interval(REFRESH_SECS, self._do_refresh)
        self.set_interval(1, self._tick)
        self._cal = load_calibration()
        self._last_activity = time.monotonic()
        self._screen_is_off = False
        if EVDEV:
            threading.Thread(target=self._touch_loop, daemon=True).start()

    def _tick(self) -> None:
        now_str = datetime.now().strftime("%H:%M:%S")
        host = socket.gethostname()
        t = Text()
        t.append(" HOMELAB", style="bold bright_green")
        t.append("//", style="dim green")
        t.append("CTRL", style="bold bright_green")
        t.append(f"   ●  {host}  ●  {now_str}", style="dim green")
        self.query_one("#topbar").update(t)
        self.query_one("#statusbar").update(
            Text(f" ► {self.status_msg}  │  Use buttons below to control containers",
                 style="dim green")
        )
        # Screen timeout
        if SCREEN_TIMEOUT > 0 and not self._screen_is_off:
            if time.monotonic() - self._last_activity > SCREEN_TIMEOUT:
                self._screen_is_off = True
                screen_off()

    # ── Activity tracking ─────────────────────────────────────────

    def on_key(self, event) -> None:
        """Any key press wakes the screen / resets the timeout."""
        self._bump_activity()

    def _bump_activity(self) -> None:
        self._last_activity = time.monotonic()
        if self._screen_is_off:
            self._screen_is_off = False
            screen_on()

    # ── Container table ───────────────────────────────────────────

    def _do_refresh(self) -> None:
        """Fetch container data in a worker thread to keep UI responsive."""
        def _fetch():
            containers, _ = get_containers()
            self.call_from_thread(self._apply_containers, containers)

        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_containers(self, containers: list[dict]) -> None:
        self._containers = containers
        tbl = self.query_one("#tbl", DataTable)
        prev_id = self.selected_id
        tbl.clear()
        restore_row = 0
        for i, c in enumerate(containers):
            up   = c["state"] == "running"
            dot  = Text("● " if up else "✖ ", style="bright_green" if up else "bright_red")
            stat = Text(c["state"], style="bright_green" if up else "bright_red")
            tbl.add_row(dot, c["name"], stat, c["status"][:22], c["image"][:26], key=c["id"])
            if c["id"] == prev_id:
                restore_row = i
        if tbl.row_count > 0:
            tbl.move_cursor(row=restore_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.selected_id = event.row_key.value
        name = next(
            (c["name"] for c in self._containers if c["id"] == self.selected_id), "?"
        )
        self.status_msg = f"Selected: {name}"

    # ── Button handlers ───────────────────────────────────────────

    @on(Button.Pressed, "#b-start")
    def _btn_start(self):    self.action_act("start")

    @on(Button.Pressed, "#b-stop")
    def _btn_stop(self):     self.action_act("stop")

    @on(Button.Pressed, "#b-restart")
    def _btn_restart(self):  self.action_act("restart")

    @on(Button.Pressed, "#b-refresh")
    def _btn_refresh(self):  self.action_refresh()

    @on(Button.Pressed, "#b-screen")
    def _btn_screen(self):
        self._screen_is_off = True
        self.status_msg = "Screen off — touch or press any key to wake"
        screen_off()

    def action_act(self, verb: str) -> None:
        if not self.selected_id:
            self.status_msg = "No container selected — use ↑↓ arrows first"
            return
        name = next(
            (c["name"] for c in self._containers if c["id"] == self.selected_id), "?"
        )
        self.status_msg = f"Running {verb} on {name}…"
        # Run the blocking API call in a worker thread
        cid = self.selected_id
        def _act():
            ok, msg = container_action(cid, verb)
            def _done():
                self.status_msg = f"{'✓' if ok else '✗'} {verb} {name}: {msg}"
                self.set_timer(1.2, self._do_refresh)
            self.call_from_thread(_done)
        threading.Thread(target=_act, daemon=True).start()

    def action_refresh(self) -> None:
        self._do_refresh()
        self.status_msg = "Refreshed."

    # ── Touch screen via evdev ────────────────────────────────────

    def _touch_loop(self) -> None:
        try:
            MT_X  = evdev.ecodes.ABS_MT_POSITION_X
            MT_Y  = evdev.ecodes.ABS_MT_POSITION_Y
            MT_ID = evdev.ecodes.ABS_MT_TRACKING_ID

            devs = [evdev.InputDevice(p) for p in evdev.list_devices()]
            dev  = None
            for d in devs:
                caps = d.capabilities()
                if evdev.ecodes.EV_ABS not in caps:
                    continue
                abs_codes = [c for c, _ in caps[evdev.ecodes.EV_ABS]]
                if MT_X in abs_codes and MT_Y in abs_codes:
                    dev = d
                    break
            if not dev:
                return

            abs_map = dict(dev.capabilities()[evdev.ecodes.EV_ABS])
            max_x   = abs_map[MT_X].max
            max_y   = abs_map[MT_Y].max
            cx = cy = 0

            for ev in dev.read_loop():
                if ev.type != evdev.ecodes.EV_ABS:
                    continue
                if ev.code == MT_X:
                    cx = ev.value
                elif ev.code == MT_Y:
                    cy = ev.value
                elif ev.code == MT_ID and ev.value != -1:
                    # Finger touched down
                    if max_x > 0 and max_y > 0:
                        xf, yf = normalize_touch(cx, cy, self._cal, max_x, max_y)
                        self.call_from_thread(self._on_touch, xf, yf)

        except Exception as exc:
            log.debug("_touch_loop exited: %s", exc)

    def _on_touch(self, xf: float, yf: float) -> None:
        """
        Called on the Textual event loop whenever a finger-down event fires.

        *** KEY FIX ***
        If the screen is currently off, the touch should ONLY wake the screen —
        it must NOT fall through and accidentally trigger a button or row.
        """
        if self._screen_is_off:
            self._bump_activity()   # wake screen, consume event
            return

        # Reset timeout on any deliberate touch
        self._bump_activity()

        try:
            sz = self.size
            if sz.width == 0 or sz.height == 0:
                return

            # Touch fraction → character cell
            cx = xf * sz.width
            cy = yf * sz.height

            # ── Action bar buttons ────────────────────────────────
            btn_actions = [
                ("b-start",   lambda: self.action_act("start")),
                ("b-stop",    lambda: self.action_act("stop")),
                ("b-restart", lambda: self.action_act("restart")),
                ("b-refresh", self.action_refresh),
                ("b-screen",  self._btn_screen),
            ]
            for btn_id, handler in btn_actions:
                try:
                    r = self.query_one(f"#{btn_id}").region
                    if r.x <= cx <= r.x + r.width and r.y <= cy <= r.y + r.height:
                        handler()
                        return
                except Exception:
                    pass

            # ── Container table rows ──────────────────────────────
            try:
                tbl_w = self.query_one("#tbl")
                r = tbl_w.region
                if r.x <= cx <= r.x + r.width and r.y <= cy <= r.y + r.height:
                    row_idx = max(0, int(cy - r.y - 2))  # subtract header + border
                    if row_idx < len(self._containers):
                        c = self._containers[row_idx]
                        self.selected_id = c["id"]
                        self.query_one("#tbl", DataTable).move_cursor(row=row_idx)
                        self.status_msg = f"Touch → {c['name']}"
            except Exception:
                pass

        except Exception as exc:
            log.debug("_on_touch error: %s", exc)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="HOMELAB//CTRL v2")
    p.add_argument("--calibrate", action="store_true",
                   help="Run interactive touch calibration and exit")
    p.add_argument("--log", action="store_true",
                   help=f"Enable debug logging to {LOG_FILE}")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.log:
        logging.basicConfig(
            filename=LOG_FILE,
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(message)s",
        )
        log.info("Logging started — writing to %s", LOG_FILE)
    else:
        logging.disable(logging.CRITICAL)  # silence everything

    if args.calibrate:
        run_calibration()
    else:
        # Warm up counters so first readings aren't 0
        psutil.cpu_percent(interval=0.1)
        net_speed()
        HomelabApp().run()