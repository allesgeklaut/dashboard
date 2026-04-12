#!/usr/bin/env python3
"""
HOMELAB//CTRL - Enhanced Terminal Dashboard
Requirements: pip install rich psutil requests
"""

import time, socket, os, requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timedelta
import psutil
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box
from rich.console import Console

# ── CONFIG ───────────────────────────────────────────────────────
# Get your API key from Portainer: Account → API keys → Add API key
PORTAINER_URL     = "https://192.168.0.43:9443"
PORTAINER_API_KEY = "ptr_8iEGnGe87u3kbreQv779aPiMqAjYRO7b6WQ7tOkGRCE="

# NFS mount points to always show (even if empty/slow)
NFS_MOUNTS = ["/mnt/nas"]

# How many containers to show in the panel
MAX_CONTAINERS = 10
# ────────────────────────────────────────────────────────────────

console = Console()

GREEN      = "bright_green"
GREEN_DIM  = "green"
AMBER      = "yellow"
RED        = "bright_red"
CYAN       = "cyan"
DIM        = "dim green"

# ── helpers ──────────────────────────────────────────────────────
def bar(pct: float, width: int = 20) -> Text:
    filled = int(pct / 100 * width)
    empty  = width - filled
    colour = RED if pct > 85 else AMBER if pct > 65 else GREEN_DIM
    b = Text()
    b.append("█" * filled, style=colour)
    b.append("░" * empty,  style="grey23")
    return b

def val_colour(pct: float) -> str:
    return RED if pct > 85 else AMBER if pct > 65 else GREEN

def fmt_bytes(val: float) -> str:
    if val > 1_000_000: return f"{val/1_000_000:.1f} MB/s"
    if val > 1_000:     return f"{val/1_000:.1f} KB/s"
    return f"{val:.0f} B/s"

_net_prev = None
_net_time = None

def net_speed():
    global _net_prev, _net_time
    n = psutil.net_io_counters()
    now = time.time()
    if _net_prev is None:
        _net_prev, _net_time = n, now
        return 0.0, 0.0
    dt = now - _net_time or 0.001
    tx = (n.bytes_sent - _net_prev.bytes_sent) / dt
    rx = (n.bytes_recv - _net_prev.bytes_recv) / dt
    _net_prev, _net_time = n, now
    return tx, rx

def temps():
    try:
        t = psutil.sensors_temperatures()
        for key in ("coretemp","cpu_thermal","k10temp","acpitz"):
            if key in t and t[key]:
                return t[key][0].current
    except Exception:
        pass
    return None

def uptime_str():
    secs = time.time() - psutil.boot_time()
    td = timedelta(seconds=int(secs))
    h, r = divmod(td.seconds, 3600)
    m, _ = divmod(r, 60)
    return f"{td.days}d {h}h {m}m"

def local_ip():
    for iface, addr_list in psutil.net_if_addrs().items():
        if iface == "lo": continue
        for a in addr_list:
            if a.family == 2 and not a.address.startswith("127."):
                return a.address
    return "n/a"

# ── Portainer API ─────────────────────────────────────────────────
_portainer_ok = False
_portainer_endpoint_id = None

def portainer_headers():
    return {"X-API-Key": PORTAINER_API_KEY}

def get_portainer_endpoint():
    global _portainer_endpoint_id
    if _portainer_endpoint_id:
        return _portainer_endpoint_id
    try:
        r = requests.get(f"{PORTAINER_URL}/api/endpoints",
                         headers=portainer_headers(), timeout=2, verify=False)
        if r.ok and r.json():
            _portainer_endpoint_id = r.json()[0]["Id"]
            return _portainer_endpoint_id
    except Exception:
        pass
    return None

def get_containers_portainer():
    global _portainer_ok
    eid = get_portainer_endpoint()
    if not eid:
        _portainer_ok = False
        return None
    try:
        r = requests.get(
            f"{PORTAINER_URL}/api/endpoints/{eid}/docker/containers/json?all=true",
            headers=portainer_headers(), timeout=3, verify=False
        )
        if r.ok:
            _portainer_ok = True
            rows = []
            for c in r.json():
                name   = c["Names"][0].lstrip("/") if c["Names"] else "?"
                state  = c["State"]   # "running", "exited", etc.
                status = c["Status"]  # "Up 2 days", "Exited (1)..."
                image  = c["Image"].split("/")[-1][:28]
                rows.append((name, state, status, image))
            return rows
        _portainer_ok = False
    except Exception:
        _portainer_ok = False
    return None

def get_containers_docker():
    import subprocess
    try:
        out = subprocess.check_output(
            ["docker", "ps", "-a", "--format",
             "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}"],
            stderr=subprocess.DEVNULL, timeout=3
        ).decode().strip()
        rows = []
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) >= 3:
                name, state = p[0], p[1]
                status = p[2] if len(p) > 2 else ""
                image  = (p[3] if len(p) > 3 else "").split("/")[-1][:28]
                rows.append((name, state, status, image))
        return rows
    except Exception:
        return []

def get_containers():
    r = get_containers_portainer()
    if r is not None:
        return r, True   # (rows, from_portainer)
    return get_containers_docker(), False

# ── Panel builders ────────────────────────────────────────────────
def header_panel() -> Panel:
    now = datetime.now().strftime("%H:%M:%S")
    src = Text()
    src.append(" VIA PORTAINER", style=GREEN_DIM) if _portainer_ok else src.append(" VIA DOCKER CLI", style=DIM)
    title = Text()
    title.append("HOMELAB", style=f"bold {GREEN}")
    title.append("//", style=DIM)
    title.append("CTRL", style=f"bold {GREEN}")
    title.append(src)
    right = Text()
    right.append("HOST: ", style=DIM)
    right.append(socket.gethostname(), style=GREEN)
    right.append("   ● LIVE   ", style=DIM)
    right.append(now, style=f"bold {CYAN}")
    t = Table.grid(expand=True)
    t.add_column(justify="left")
    t.add_column(justify="right")
    t.add_row(title, right)
    return Panel(t, style=GREEN_DIM, box=box.HEAVY, padding=(0,1))

def resources_panel() -> Panel:
    cpu  = psutil.cpu_percent(interval=None)
    mem  = psutil.virtual_memory()
    ram  = mem.percent
    swap = psutil.swap_memory().percent
    tmp  = temps()
    load1, load5, load15 = psutil.getloadavg()
    cpu_count = psutil.cpu_count() or 1
    # load as % of cpu count for bar scaling
    load1_pct  = min(load1  / cpu_count * 100, 100)
    load5_pct  = min(load5  / cpu_count * 100, 100)
    load15_pct = min(load15 / cpu_count * 100, 100)

    t = Table.grid(padding=(0,1))
    t.add_column(width=12, style=DIM)
    t.add_column()
    t.add_column(width=16, justify="right")

    def row(label, pct, display):
        t.add_row(f"  {label}", bar(pct), Text(display, style=val_colour(pct)))

    row("CPU",     cpu,  f"{cpu:.0f}%")
    row("RAM",     ram,  f"{ram:.0f}%  {mem.used/1e9:.1f}/{mem.total/1e9:.1f}GB")
    row("SWAP",    swap, f"{swap:.0f}%")
    if tmp:
        row("TEMP", min(tmp, 100), f"{tmp:.0f}°C")

    # divider
    t.add_row(Text("  ─── LOAD AVG ───", style=DIM), Text(""), Text(""))

    row("1  min",  load1_pct,  f"{load1:.2f}")
    row("5  min",  load5_pct,  f"{load5:.2f}")
    row("15 min",  load15_pct, f"{load15:.2f}")

    return Panel(
        t, title=Text("► RESOURCES", style=GREEN_DIM),
        title_align="left", style=GREEN_DIM,
        box=box.SIMPLE_HEAVY, padding=(0,0)
    )

def disk_panel() -> Panel:
    t = Table.grid(padding=(0,1))
    t.add_column(width=20, style=DIM)
    t.add_column()
    t.add_column(width=18, justify="right")

    shown = set()

    # NFS mounts first
    for mp in NFS_MOUNTS:
        if not os.path.ismount(mp):
            t.add_row(
                Text(f"  {mp[:18]}", style=DIM),
                Text("not mounted", style=DIM),
                Text("NFS", style=DIM)
            )
            shown.add(mp)
            continue
        try:
            u = psutil.disk_usage(mp)
            pct = u.percent
            t.add_row(
                Text(f"  {mp[:18]}", style=GREEN_DIM),
                bar(pct, 16),
                Text(f"{u.used/1e9:.0f}/{u.total/1e9:.0f}GB  {pct:.0f}%  NFS",
                     style=val_colour(pct))
            )
            shown.add(mp)
        except Exception:
            t.add_row(Text(f"  {mp[:18]}", style=DIM),
                      Text("error", style=RED), Text("NFS", style=DIM))
            shown.add(mp)

    # Local partitions
    count = 0
    for p in psutil.disk_partitions(all=False):
        if p.mountpoint in shown or count >= 4: continue
        if any(x in p.fstype for x in ("tmpfs","devtmp","squash","overlay","proc")): continue
        try:
            u = psutil.disk_usage(p.mountpoint)
            if u.total < 100_000_000: continue  # skip tiny (<100MB)
            pct = u.percent
            label = f"{p.mountpoint[:16]} [{p.fstype}]"
            t.add_row(
                Text(f"  {label[:20]}", style=DIM),
                bar(pct, 16),
                Text(f"{u.used/1e9:.0f}/{u.total/1e9:.0f}GB  {pct:.0f}%",
                     style=val_colour(pct))
            )
            shown.add(p.mountpoint)
            count += 1
        except Exception:
            continue

    return Panel(
        t, title=Text("► STORAGE", style=GREEN_DIM),
        title_align="left", style=GREEN_DIM,
        box=box.SIMPLE_HEAVY, padding=(0,0)
    )

def network_panel(tx: float, rx: float) -> Panel:
    t = Table.grid(padding=(0,2))
    t.add_column(width=12, style=DIM)
    t.add_column(style=GREEN)

    t.add_row("  LAN IP",   local_ip())
    t.add_row("  ▲ TX",     Text(fmt_bytes(tx), style=GREEN if tx<5e6 else AMBER))
    t.add_row("  ▼ RX",     Text(fmt_bytes(rx), style=GREEN if rx<5e6 else AMBER))
    t.add_row("  UPTIME",   uptime_str())
    t.add_row("  PORTAINER",Text("CONNECTED" if _portainer_ok else "FALLBACK → CLI",
                                 style=GREEN if _portainer_ok else AMBER))

    return Panel(
        t, title=Text("► NETWORK", style=GREEN_DIM),
        title_align="left", style=GREEN_DIM,
        box=box.SIMPLE_HEAVY, padding=(0,0)
    )

def docker_panel() -> Panel:
    containers, from_portainer = get_containers()

    src_label = "VIA PORTAINER" if from_portainer else "VIA DOCKER CLI"
    t = Table(
        box=box.SIMPLE_HEAVY, style=GREEN_DIM,
        show_header=True, header_style=f"bold {GREEN_DIM}",
        padding=(0,1), expand=True
    )
    t.add_column("CONTAINER", style=GREEN, no_wrap=True, max_width=22)
    t.add_column("STATUS",    width=22, no_wrap=True)
    t.add_column("IMAGE",     style=DIM, no_wrap=True, max_width=28)

    if not containers:
        t.add_row(Text("no containers found", style=DIM), "", "")
    else:
        for row in containers[:MAX_CONTAINERS]:
            name, state, status, image = row
            running = state == "running"
            st = Text()
            st.append("● " if running else "✖ ", style=GREEN if running else RED)
            st.append(status[:20], style=GREEN_DIM if running else RED)
            t.add_row(name, st, image)

    return Panel(
        t,
        title=Text(f"► DOCKER CONTAINERS  [{src_label}]", style=GREEN_DIM),
        title_align="left", style=GREEN_DIM,
        box=box.SIMPLE_HEAVY, padding=(0,0)
    )

# ── layout ────────────────────────────────────────────────────────
def make_layout(tx: float, rx: float) -> Layout:
    root = Layout()
    root.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )
    root["body"].split_row(
        Layout(name="left",  ratio=2),
        Layout(name="right", ratio=3),
    )
    root["left"].split_column(
        Layout(name="res", ratio=4),
        Layout(name="net", ratio=2),
    )
    root["right"].split_column(
        Layout(name="disk",   ratio=2),
        Layout(name="docker", ratio=3),
    )
    root["header"].update(header_panel())
    root["res"].update(resources_panel())
    root["net"].update(network_panel(tx, rx))
    root["disk"].update(disk_panel())
    root["docker"].update(docker_panel())

    footer = Text(justify="center")
    footer.append(f"  {socket.gethostname()} ", style=DIM)
    footer.append("· CTRL+C TO EXIT · NFS MOUNTS: ", style=DIM)
    footer.append(", ".join(NFS_MOUNTS), style=GREEN_DIM)
    root["footer"].update(footer)
    return root

# ── main ──────────────────────────────────────────────────────────
def main():
    psutil.cpu_percent(interval=0.1)
    net_speed()
    # pre-fetch portainer endpoint
    get_portainer_endpoint()

    console.clear()
    try:
        with Live(make_layout(0, 0), refresh_per_second=2,
                  screen=True, console=console) as live:
            while True:
                tx, rx = net_speed()
                live.update(make_layout(tx, rx))
                time.sleep(0.5)
    except KeyboardInterrupt:
        console.clear()
        console.print(Text("HOMELAB//CTRL terminated.", style=GREEN_DIM))

if __name__ == "__main__":
    main()

