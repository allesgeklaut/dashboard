"""
homelab_core.py — shared data-fetching helpers for HOMELAB//CTRL
Used by both main.py (FastAPI backend) and dashboard.py (Textual TUI).
"""
from __future__ import annotations
import os, socket, subprocess, threading, time
from datetime import timedelta

import psutil, requests, urllib3
from dotenv import load_dotenv

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ────────────────────────────────────────────────────────────────────
PORTAINER_URL     = os.getenv("PORTAINER_URL", "https://192.168.0.43:9443")
PORTAINER_API_KEY = os.getenv("PORTAINER_API_KEY", "")
PORTAINER_ENVS: list[int] | None = None  # None = auto-discover; or e.g. [1, 2]
ADGUARD_URL  = os.getenv("ADGUARD_URL", "http://192.168.0.2")
ADGUARD_USER = os.getenv("ADGUARD_USER", "")
ADGUARD_PASS = os.getenv("ADGUARD_PASS", "")
NFS_MOUNTS    = [m.strip() for m in os.getenv("NFS_MOUNTS", "/mnt/nas").split(",") if m.strip()]
SHELLY_PLUG_URL = os.getenv("SHELLY_PLUG_URL", "http://192.168.0.61")

_HDR = {"X-API-Key": PORTAINER_API_KEY}

# ── Net speed ─────────────────────────────────────────────────────────────────
_prev_net   = None
_prev_net_t = None
_net_lock   = threading.Lock()

def net_speed() -> tuple[float, float]:
    """Return (tx_bytes/s, rx_bytes/s) since last call."""
    global _prev_net, _prev_net_t
    with _net_lock:
        n, now = psutil.net_io_counters(), time.monotonic()
        if _prev_net is None:
            _prev_net, _prev_net_t = n, now
            return 0.0, 0.0
        dt = (now - _prev_net_t) or 1e-3
        tx = (n.bytes_sent - _prev_net.bytes_sent) / dt
        rx = (n.bytes_recv - _prev_net.bytes_recv) / dt
        _prev_net, _prev_net_t = n, now
        return tx, rx

# ── Portainer endpoint discovery ──────────────────────────────────────────────
_eids:      list[int] | None = None
_env_names: dict[int, str]   = {}
_eid_lock   = threading.Lock()

def get_eids() -> list[int]:
    global _eids, _env_names
    with _eid_lock:
        if _eids is not None:
            return _eids
    if PORTAINER_ENVS:
        with _eid_lock:
            _eids = list(PORTAINER_ENVS)
        return _eids
    try:
        r = requests.get(
            f"{PORTAINER_URL}/api/endpoints",
            headers=_HDR, timeout=3, verify=False,
        )
        if r.ok and r.json():
            with _eid_lock:
                _eids      = [e["Id"]   for e in r.json()]
                _env_names = {e["Id"]: e["Name"] for e in r.json()}
            return _eids
    except Exception:
        pass
    return []

def get_env_names() -> dict[int, str]:
    """Return {endpoint_id: endpoint_name} mapping (populated after get_eids())."""
    get_eids()
    return _env_names

# ── Container listing ─────────────────────────────────────────────────────────

def _parse_container(c: dict, host: str, eid: int) -> dict:
    return {
        "id":     c["Id"][:12],
        "name":   c["Names"][0].lstrip("/"),
        "state":  c["State"],
        "status": c["Status"],
        "image":  c["Image"].split("/")[-1].split(":")[0],
        "host":   host,
        "eid":    eid,
    }

def portainer_containers() -> list[dict]:
    """Fetch all containers from all discovered Portainer endpoints."""
    out: list[dict] = []
    for eid in get_eids():
        try:
            r = requests.get(
                f"{PORTAINER_URL}/api/endpoints/{eid}/docker/containers/json?all=true",
                headers=_HDR, timeout=4, verify=False,
            )
            if r.ok:
                host = _env_names.get(eid, str(eid))
                out.extend(_parse_container(c, host, eid) for c in r.json())
        except Exception:
            pass
    return out

def cli_containers() -> list[dict]:
    """Fallback: list containers via the local docker CLI."""
    try:
        raw = subprocess.check_output(
            ["docker", "ps", "-a", "--format",
             "{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}"],
            timeout=4, stderr=subprocess.DEVNULL,
        ).decode().strip()
        out = []
        for line in raw.splitlines():
            p = line.split("\t")
            if len(p) < 4:
                continue
            out.append({
                "id":     p[0],
                "name":   p[1],
                "state":  p[2],
                "status": p[3],
                "image":  (p[4] if len(p) > 4 else "").split("/")[-1].split(":")[0],
                "host":   "local",
                "eid":    None,
            })
        return out
    except Exception:
        return []

def get_containers() -> tuple[list[dict], str]:
    """Return (containers, source) where source is 'portainer', 'docker', or 'none'."""
    containers = portainer_containers()
    if containers:
        return containers, "portainer"
    containers = cli_containers()
    if containers:
        return containers, "docker"
    return [], "none"

# ── Container actions ─────────────────────────────────────────────────────────

def container_action(cid: str, action: str, eid: int | None = None) -> tuple[bool, str]:
    """Start, stop, or restart a container via Portainer or local docker CLI."""
    if eid is None:
        eids = get_eids()
        eid  = eids[0] if eids else None
    if eid is not None:
        try:
            r = requests.post(
                f"{PORTAINER_URL}/api/endpoints/{eid}/docker/containers/{cid}/{action}",
                headers=_HDR, timeout=15, verify=False,
            )
            if r.status_code in (200, 204, 304):
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

# ── System stats ──────────────────────────────────────────────────────────────

def get_temp() -> float | None:
    """Return CPU temperature in °C, or None if unavailable."""
    try:
        sensors = psutil.sensors_temperatures()
        for key in ("coretemp", "cpu_thermal", "k10temp", "acpitz"):
            if key in sensors and sensors[key]:
                return sensors[key][0].current
    except Exception:
        pass
    return None

def get_ip() -> str:
    """Return the first non-loopback IPv4 address, or 'n/a'."""
    for iface, addrs in psutil.net_if_addrs().items():
        if iface == "lo":
            continue
        for a in addrs:
            if a.family == 2 and not a.address.startswith("127."):
                return a.address
    return "n/a"

def get_system_stats() -> dict:
    """Return a dict of current CPU, RAM, swap, load, net, temp, uptime, and IP."""
    cpu = psutil.cpu_percent()
    mem = psutil.virtual_memory()
    swp = psutil.swap_memory()
    l1, l5, l15 = psutil.getloadavg()
    tx, rx = net_speed()
    temp = get_temp()
    td   = timedelta(seconds=int(time.time() - psutil.boot_time()))
    h, rem = divmod(td.seconds, 3600)
    m = rem // 60
    return {
        "cpu":          cpu,
        "ram_pct":      mem.percent,
        "ram_used_gb":  round(mem.used  / 1024**3, 1),
        "ram_total_gb": round(mem.total / 1024**3, 1),
        "swap_pct":     swp.percent,
        "temp":         round(temp, 1) if temp is not None else None,
        "load1":        round(l1,  2),
        "load5":        round(l5,  2),
        "load15":       round(l15, 2),
        "net_tx":       tx,
        "net_rx":       rx,
        "uptime":       f"{td.days}d {h:02d}h {m:02d}m",
        "hostname":     socket.gethostname(),
        "ip":           get_ip(),
    }

# ── Storage ───────────────────────────────────────────────────────────────────

_SKIP_FS = ("tmpfs", "devtmpfs", "squashfs", "overlay", "efi")

def get_storage() -> list[dict]:
    """Return disk usage for NFS mounts and local partitions."""
    out, seen = [], set()
    for mp in NFS_MOUNTS:
        if not os.path.ismount(mp):
            out.append({"mount": mp, "error": "not mounted", "type": "NFS"})
            seen.add(mp)
            continue
        try:
            u = psutil.disk_usage(mp)
            out.append({"mount": mp, "used_gb": round(u.used/1024**3, 1),
                        "total_gb": round(u.total/1024**3, 1),
                        "percent": u.percent, "type": "NFS"})
            seen.add(mp)
        except Exception:
            out.append({"mount": mp, "error": "read error", "type": "NFS"})
    cnt = 0
    for p in psutil.disk_partitions(all=False):
        if p.mountpoint in seen or cnt >= 4:
            continue
        if any(x in p.fstype for x in _SKIP_FS):
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
            if u.total < 1e8:
                continue
            out.append({"mount": p.mountpoint, "used_gb": round(u.used/1024**3, 1),
                        "total_gb": round(u.total/1024**3, 1),
                        "percent": u.percent, "type": p.fstype})
            seen.add(p.mountpoint)
            cnt += 1
        except Exception:
            continue
    return out

# ── AdGuard ───────────────────────────────────────────────────────────────────

def get_adguard_stats() -> dict:
    """Return AdGuard Home query stats, or {'error': ...} on failure."""
    try:
        r = requests.get(
            f"{ADGUARD_URL}/control/stats",
            auth=(ADGUARD_USER, ADGUARD_PASS), timeout=3,
        )
        if r.ok:
            d       = r.json()
            total   = d.get("num_dns_queries", 0)
            blocked = d.get("num_blocked_filtering", 0)
            return {
                "avg_ms":      round(d.get("avg_processing_time", 0) * 1000, 1),
                "queries":     total,
                "blocked":     blocked,
                "blocked_pct": round(blocked / total * 100 if total else 0, 1),
            }
    except Exception:
        pass
    return {"error": "unavailable"}


# ── Shelly Plus Plug ──────────────────────────────────────────────────────────

def get_shelly_stats() -> dict:
    """Return power stats from a Shelly Plus Plug (Gen2) via local REST API.

    Endpoint: GET /rpc/Switch.GetStatus?id=0
    Returns keys: output (bool), apower (W), voltage (V), current (A),
    or {'error': ...} on failure.
    """
    try:
        r = requests.get(
            f"{SHELLY_PLUG_URL}/rpc/Switch.GetStatus?id=0",
            timeout=3,
        )
        if r.ok:
            d = r.json()
            return {
                "output":  d.get("output", False),
                "apower":  round(d.get("apower",  0.0), 1),
                "voltage": round(d.get("voltage", 0.0), 1),
                "current": round(d.get("current", 0.0), 3),
            }
    except Exception:
        pass
    return {"error": "unavailable"}

# ── Startup helper ────────────────────────────────────────────────────────────

def prime_counters() -> None:
    """Call once at startup to initialise rolling counters."""
    psutil.cpu_percent(interval=0.1)
    net_speed()
