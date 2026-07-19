"""
homelab_core.py — shared data-fetching helpers for HOMELAB//CTRL
Used by both main.py (FastAPI backend) and dashboard.py (Textual TUI).
"""
from __future__ import annotations
import os, socket, subprocess, threading, time, json
import paramiko
from datetime import timedelta

import psutil, requests, urllib3
from dotenv import load_dotenv

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ────────────────────────────────────────────────────────────────────
PORTAINER_URL     = os.getenv("PORTAINER_URL", "https://192.168.0.46:9443")
PORTAINER_API_KEY = os.getenv("PORTAINER_API_KEY", "")
PORTAINER_ENVS: list[int] | None = None  # None = auto-discover; or e.g. [1, 2]
ADGUARD_URL  = os.getenv("ADGUARD_URL")          # None if not set → feature disabled
ADGUARD_USER = os.getenv("ADGUARD_USER", "")
ADGUARD_PASS = os.getenv("ADGUARD_PASS", "")
NFS_MOUNTS    = [m.strip() for m in os.getenv("NFS_MOUNTS", "/mnt/nas").split(",") if m.strip()]
SHELLY_PLUG_URL   = os.getenv("SHELLY_PLUG_URL")    # None if not set → feature disabled
SHELLY_PLUG_2_URL = os.getenv("SHELLY_PLUG_2_URL")  # None if not set → feature disabled
OLLAMA_URL = os.getenv("OLLAMA_URL")               # None if not set → feature disabled

# ── Wake‑on‑LAN Config ───────────────────────────────────────────────────────
WOL_TARGET_MAC    = os.getenv("WOL_TARGET_MAC", "").lower()  # MAC address of target machine (e.g., "aa:bb:cc:dd:ee:ff")
WOL_BROADCAST_IP  = os.getenv("WOL_BROADCAST_IP", "255.255.255.255")  # Broadcast IP for magic packet
WOL_PORT          = int(os.getenv("WOL_PORT", "9000"))  # UDP port for WOL packets (typically 7 or 9000)

# ── SSH Config ───────────────────────────────────────────────────────────────
SSH_USER = os.getenv("SSH_USER", "woladmin")
SSH_KEY_PATH = os.getenv("SSH_PRIVATE_KEY_PATH", "")

_HDR = {"X-API-Key": PORTAINER_API_KEY}


# ── Static system info (probed once at startup) ──────────────────────────────
# Many helpers only need *which* sensor / device / mount point to read, not the
# live value.  Doing that detection on every poll is wasted work — interfaces
# and hwmon paths are static for the lifetime of the process.  This dataclass
# caches all the cheap-to-compute, slow-to-change facts so the hot-path
# getters only read the live counter.

from dataclasses import dataclass, field

@dataclass
class _GpuPaths:
    """Cached sysfs paths for a single detected AMD GPU.

    ``None`` for any path that doesn't exist on the host (e.g. some cards
    don't expose fan1_input).
    """
    hwmon_path:    str | None
    card_path:     str | None
    temp_junction: str | None = None   # temp2_input (preferred)
    temp_edge:     str | None = None   # temp1_input (fallback)
    fan_rpm:       str | None = None   # fan1_input
    fan_pwm:       str | None = None   # pwm1
    power:         str | None = None   # power1_average (µW)
    usage:         str | None = None   # gpu_busy_percent
    vram_total:    str | None = None   # mem_info_vram_total (bytes)
    vram_used:     str | None = None   # mem_info_vram_used  (bytes)

    def live_paths(self) -> list[str]:
        """Sysfs files that the live getters will actually read."""
        return [p for p in (
            self.temp_junction, self.temp_edge, self.fan_rpm,
            self.fan_pwm, self.power, self.usage,
            self.vram_total, self.vram_used,
        ) if p is not None]


@dataclass
class SystemInfo:
    """Process-wide cache of static OS / hardware facts.

    Populated lazily on first use of :func:`get_system_info` and held as a
    module-level singleton.  All values are safe to read concurrently.
    """
    boot_time: float = 0.0
    host_ipv4: str = "n/a"
    cpu_temp_sensor: tuple[str, int] | None = None   # (key, index in sensors[key])

    # /mnt/nas etc. — only those that actually exist as a directory
    nfs_mounts: list[str] = field(default_factory=list)

    # Pre-resolved AMD GPU sysfs paths (None if no AMD GPU is present)
    amd_gpu: _GpuPaths | None = None

    # ── probing ─────────────────────────────────────────────────────────────

    @classmethod
    def probe(cls) -> "SystemInfo":
        self = cls()
        self.boot_time       = psutil.boot_time()
        self.host_ipv4       = self._detect_ip()
        self.cpu_temp_sensor = self._detect_cpu_temp_sensor()
        self.nfs_mounts      = self._detect_nfs_mounts()
        self.amd_gpu         = self._detect_amd_gpu()
        return self

    @staticmethod
    def _detect_ip() -> str:
        for iface, addrs in psutil.net_if_addrs().items():
            if iface == "lo":
                continue
            for a in addrs:
                if a.family == 2 and not a.address.startswith("127."):
                    return a.address
        return "n/a"

    @staticmethod
    def _detect_cpu_temp_sensor() -> tuple[str, int] | None:
        """Pick the first available sensor in our preferred order."""
        try:
            sensors = psutil.sensors_temperatures()
        except Exception:
            return None
        for key in ("coretemp", "cpu_thermal", "k10temp", "acpitz"):
            arr = sensors.get(key)
            if arr:
                return key, 0
        return None

    @staticmethod
    def _detect_nfs_mounts() -> list[str]:
        out: list[str] = []
        for mp in NFS_MOUNTS:
            if os.path.isdir(mp):
                out.append(mp)
        return out

    @staticmethod
    def _detect_amd_gpu() -> "_GpuPaths | None":
        """Find amdgpu hwmon + the matching drm card; pre-resolve every path
        we will need so the live getter is just a series of ``open()`` calls.
        """
        import glob

        # 1) amdgpu hwmon
        hwmon_path: str | None = None
        for cand in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            try:
                if open(f"{cand}/name").read().strip() == "amdgpu":
                    hwmon_path = cand
                    break
            except Exception:
                continue
        if hwmon_path is None:
            return None

        # 2) drm card bound to the AMD driver (prefer non-boot display)
        card_path: str | None = None
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
            try:
                if open(f"{card}/device/vendor").read().strip() != "0x1002":
                    continue
                boot_vga = open(f"{card}/device/boot_vga").read().strip() == "1"
                if not boot_vga:
                    card_path = card
                    break
                if card_path is None:
                    card_path = card
            except Exception:
                continue

        def _exists(p: str) -> str | None:
            return p if os.path.exists(p) else None

        paths = _GpuPaths(
            hwmon_path    = hwmon_path,
            card_path     = card_path,
            temp_junction = _exists(f"{hwmon_path}/temp2_input"),
            temp_edge     = _exists(f"{hwmon_path}/temp1_input"),
            fan_rpm       = _exists(f"{hwmon_path}/fan1_input"),
            fan_pwm       = _exists(f"{hwmon_path}/pwm1"),
            power         = _exists(f"{hwmon_path}/power1_average"),
            usage         = _exists(f"{card_path}/device/gpu_busy_percent") if card_path else None,
            vram_total    = _exists(f"{card_path}/device/mem_info_vram_total") if card_path else None,
            vram_used     = _exists(f"{card_path}/device/mem_info_vram_used") if card_path else None,
        )
        if not paths.live_paths():
            return None
        return paths


# Singleton — populated on first call to get_system_info()
_system_info: SystemInfo | None = None
_system_info_lock = threading.Lock()

def get_system_info() -> SystemInfo:
    """Return the process-wide :class:`SystemInfo` singleton (probed once)."""
    global _system_info
    if _system_info is not None:
        return _system_info
    with _system_info_lock:
        if _system_info is None:
            _system_info = SystemInfo.probe()
    return _system_info

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
    seen_ports: set[str] = set()
    ports: list[str] = []
    for p in c.get("Ports", []):
        pub  = p.get("PublicPort")
        priv = p.get("PrivatePort")
        proto = p.get("Type", "tcp")
        if pub:
            entry = f"{pub}\u2192{priv}/{proto}"
            if entry not in seen_ports:
                seen_ports.add(entry)
                ports.append(entry)
    return {
        "id":     c["Id"][:12],
        "name":   c["Names"][0].lstrip("/"),
        "state":  c["State"],
        "status": c["Status"],
        "image":  c["Image"].split("/")[-1].split(":")[0],
        "host":   host,
        "eid":    eid,
        "ports":  ports,
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
    sensor = get_system_info().cpu_temp_sensor
    if sensor is None:
        return None
    key, idx = sensor
    try:
        return psutil.sensors_temperatures()[key][idx].current
    except Exception:
        return None

def get_ip() -> str:
    """Return the first non-loopback IPv4 address, or 'n/a'."""
    return get_system_info().host_ipv4

def get_system_stats() -> dict:
    """Return a dict of current CPU, RAM, swap, load, net, temp, uptime, and IP."""
    info = get_system_info()
    cpu = psutil.cpu_percent()
    mem = psutil.virtual_memory()
    swp = psutil.swap_memory()
    l1, l5, l15 = psutil.getloadavg()
    tx, rx = net_speed()
    temp = get_temp()
    td   = timedelta(seconds=int(time.time() - info.boot_time))
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
        "ip":           info.host_ipv4,
    }

# ── Storage ───────────────────────────────────────────────────────────────────

_SKIP_FS = ("tmpfs", "devtmpfs", "squashfs", "overlay", "efi")

def get_storage() -> list[dict]:
    """Return disk usage for NFS mounts and local partitions."""
    out, seen = [], set()
    mounts = get_system_info().nfs_mounts
    for mp in mounts:
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
    if not ADGUARD_URL:
        return {"error": "not configured"}
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
# Energy tracker (180 s loop):  accumulates by_minute kWh into _energy_data
#                                and persists to disk — nothing else.
# get_shelly_stats():            always fetches live data fresh from the plug,
#                                then merges today/yesterday kWh from memory.
# This keeps the live display snappy while energy bookkeeping stays accurate.

import json
import datetime as _dt

_ENERGY_FILE = os.getenv("HOMELAB_ENERGY_FILE", os.path.expanduser("~/.homelab_energy.json"))

# In-memory energy accumulator.  Only the background energy-tracker thread
# writes to this dict, and the request handler only reads it.  Single-process,
# so no lock is needed.
_energy_data: dict = {
    "today":           "",    # YYYY-MM-DD
    "today_wh":        0.0,
    "yesterday":       "",    # YYYY-MM-DD
    "yesterday_wh":    0.0,
    "_last_minute_ts": 0,     # unix ts of last processed minute
}


def _check_energy_file() -> None:
    """Warn once if the energy file path is a directory (common mistake when
    a Docker volume mount created a directory at that path) or otherwise
    not writable."""
    if os.path.isdir(_ENERGY_FILE):
        print(
            f"[homelab_core] WARNING: HOMELAB_ENERGY_FILE={_ENERGY_FILE!r} is a "
            f"directory; energy data will NOT be persisted. Remove the directory "
            f"(or fix the volume mount) and restart the container.",
            flush=True,
        )


def _load_energy() -> None:
    try:
        with open(_ENERGY_FILE) as f:
            saved = json.load(f)
        _energy_data.update(saved)
    except FileNotFoundError:
        pass
    except (IsADirectoryError, PermissionError) as exc:
        print(f"[homelab_core] energy file not readable: {exc}", flush=True)
    except Exception:
        pass


def _save_energy() -> None:
    try:
        with open(_ENERGY_FILE, "w") as f:
            json.dump(_energy_data, f, indent=2)
    except (IsADirectoryError, PermissionError) as exc:
        # Common when the volume mount created a directory at this path.
        # We only log the first time.
        print(f"[homelab_core] energy file not writable: {exc}", flush=True)
    except Exception:
        pass


def _accumulate(by_minute: list, minute_ts: int) -> None:
    """Add newly seen per-minute mWh values to today's Wh accumulator."""
    if not by_minute or not minute_ts:
        return
    today_str = _dt.date.today().isoformat()
    last_ts = _energy_data["_last_minute_ts"]

    # How many fresh minutes does this payload contain?
    if last_ts:
        new_mins = min(len(by_minute), round((minute_ts - last_ts) / 60))
    else:
        new_mins = len(by_minute)   # first ever poll — use all 3
    if new_mins <= 0:
        return

    # Midnight rollover
    if _energy_data["today"] != today_str:
        if _energy_data["today"]:   # not the very first run
            _energy_data["yesterday"]    = _energy_data["today"]
            _energy_data["yesterday_wh"] = _energy_data["today_wh"]
        _energy_data["today"]    = today_str
        _energy_data["today_wh"] = 0.0

    # by_minute[0] = most recent complete minute, [1] = one before, etc.
    added_wh = sum(by_minute[:new_mins]) / 1000.0   # mWh → Wh
    _energy_data["today_wh"]        = round(_energy_data["today_wh"] + added_wh, 3)
    _energy_data["_last_minute_ts"] = minute_ts


def _energy_tracker_loop() -> None:
    """Background loop: poll every 180 s purely for energy accumulation."""
    if not SHELLY_PLUG_URL:
        return
    _check_energy_file()
    _load_energy()
    while True:
        time.sleep(180)
        try:
            r = requests.get(
                f"{SHELLY_PLUG_URL}/rpc/Switch.GetStatus?id=0",
                timeout=5,
            )
            if r.ok:
                ae = r.json().get("aenergy", {})
                _accumulate(ae.get("by_minute", []), ae.get("minute_ts", 0))
                _save_energy()
        except Exception:
            pass


def start_energy_tracker() -> None:
    """Start the background energy accumulation thread.
    Call once at startup in main.py and/or dashboard.py.
    """
    threading.Thread(target=_energy_tracker_loop, daemon=True).start()


def get_shelly_stats() -> dict:
    """Fetch live Shelly stats and merge in today/yesterday kWh from memory."""
    if not SHELLY_PLUG_URL:
        return {"error": "not configured"}
    try:
        r = requests.get(
            f"{SHELLY_PLUG_URL}/rpc/Switch.GetStatus?id=0",
            timeout=3,
        )
        if r.ok:
            d = r.json()
            today_kwh     = round(_energy_data["today_wh"]     / 1000, 4)
            yesterday_kwh = round(_energy_data["yesterday_wh"] / 1000, 4)
            yesterday_str = _energy_data["yesterday"]
            return {
                "output":         d.get("output", False),
                "apower":         round(d.get("apower",  0.0), 1),
                "voltage":        round(d.get("voltage", 0.0), 1),
                "current":        round(d.get("current", 0.0), 3),
                "today_kwh":      today_kwh,
                "yesterday_kwh":  yesterday_kwh if yesterday_str else None,
                "yesterday_date": yesterday_str,
            }
    except Exception:
        pass
    return {"error": "unavailable"}



def shelly_power_cycle(shelly_url: str, delay_s: int = 10) -> tuple[bool, str]:
    """Turn the Shelly plug off then back on after delay_s seconds.

    Uses Switch.Set with toggle_after so the timer runs ON the device itself —
    it will restore power even if the network (e.g. the router) is rebooting.
    """
    try:
        r = requests.get(
            f"{shelly_url}/rpc/Switch.Set",
            params={"id": 0, "on": "false", "toggle_after": delay_s},
            timeout=5,
        )
        if r.ok:
            return True, f"off → on in {delay_s}s"
        return False, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, str(exc)


# ── Shelly Plug 2 (simple on/off, no energy tracking) ─────────────────────────

def get_shelly2_state() -> dict:
    """Return {output: bool} for the second Shelly plug, or {error: ...}."""
    if not SHELLY_PLUG_2_URL:
        return {"error": "not configured"}
    try:
        r = requests.get(
            f"{SHELLY_PLUG_2_URL}/rpc/Switch.GetStatus?id=0",
            timeout=3,
        )
        if r.ok:
            d = r.json()
            return {"output": d.get("output", False)}
    except Exception:
        pass
    return {"error": "unavailable"}

def shelly2_toggle() -> tuple[bool, str]:
    """Toggle the second Shelly plug on/off."""
    try:
        r = requests.get(
            f"{SHELLY_PLUG_2_URL}/rpc/Switch.Toggle?id=0",
            timeout=5,
        )
        if r.ok:
            output = r.json().get("output", None)
            label = "on" if output else "off"
            return True, f"plug 2 → {label}"
        return False, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, str(exc)


# ── Wake-on-LAN (WoL) ───────────────────────────────────────────────────────

def _pack_mac(mac_str):
    """Convert MAC string like 'aa:bb:cc:dd:ee:ff' to bytes."""
    if not mac_str or len(mac_str.replace(":", "")) != 12:
        return None
    try:
        # Remove any separators and convert to hex bytes
        clean = mac_str.replace(":", "").replace("-", "")
        return bytes.fromhex(clean)
    except Exception:
        return None

def _build_magic_packet(mac_bytes):
    """Build magic packet: 6 bytes of broadcast + 1598 repetitions (273 packets total)."""
    if not mac_bytes or len(mac_bytes) != 6:
        return b""
    
    packet = b"\xff" * 6 + mac_bytes * 16
    return packet

def is_target_on(host: str) -> tuple[bool, str]:
    """Return ``(True, "online")`` if the host responds to ping.

    The previous implementation attempted an SSH connection which can fail
    when key authentication is not set up.  For a simple online check we only
    need ICMP reachability.
    """
    if _ping(host):
        return True, "online"
    return False, f"{host} did not respond to ping"

def remote_shutdown(host: str, password: str | None = None) -> tuple[bool, str]:
    """SSH into *host* and run ``sudo shutdown -h now``.

    Requires that the SSH user has password‑less sudo rights for shutdown.
    Returns ``(True, "OK")`` on success or ``(False, error_message)``.
    """
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        key = None
        if SSH_KEY_PATH and os.path.exists(SSH_KEY_PATH):
            ext = os.path.splitext(SSH_KEY_PATH)[1].lower()
            try:
                if ext in ('.pem', '.pub') or SSH_KEY_PATH.endswith('id_rsa'):
                    key = paramiko.RSAKey.from_private_key_file(SSH_KEY_PATH)
                else:  # assume Ed25519
                    key = paramiko.Ed25519Key.from_private_key_file(SSH_KEY_PATH)
            except Exception:
                key = None
        client.connect(hostname=host, username=SSH_USER, pkey=key,
                       timeout=5, banner_timeout=5)
        # Build command with optional password
        if password:
            cmd = f'echo "{password}" | sudo -S shutdown -h now'
        else:
            cmd = 'sudo shutdown -h now'
        stdin, stdout, stderr = client.exec_command(cmd, timeout=3)
        err = stderr.read().decode()
        out = stdout.read().decode()
        client.close()
        if err:
            return False, err.strip()
        return True, "shutdown command sent"
    except Exception as exc:
        return False, str(exc)

def _ping(host: str, timeout: int = 2) -> bool:
    """Return True if the given hostname or IP responds to ICMP ping.

    The function accepts either a fully qualified domain name or an IPv4/IPv6
    address.  It uses the system ``ping`` command which is available on Linux
    and macOS.  If the host string contains non‑numeric characters it will be
    treated as a hostname.
    """
    try:
        cmd = ["ping", "-c", str(timeout), host]
        subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False
    

def wol_send(mac_str):
    """Send Wake-on-LAN magic packet to wake up a target machine.
    
    Returns: tuple[bool, str] — (success, message)
    """
    if not WOL_TARGET_MAC or not WOL_BROADCAST_IP:
        return False, "WOL not configured: set WOL_TARGET_MAC and optionally WOL_BROADCAST_IP"
    
    mac_bytes = _pack_mac(WOL_TARGET_MAC.upper())
    if not mac_bytes:
        return False, f"Invalid MAC address format for target machine: {WOL_TARGET_MAC}"
    
    packet = _build_magic_packet(mac_bytes)
    if not packet:
        return False, "Failed to build magic packet"
    
    try:
        # Send UDP broadcast packet (1500 bytes max is fine; ours is ~162 bytes)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        
        # Send the packet
        sock.sendto(packet, (WOL_BROADCAST_IP, WOL_PORT))
        
        return True, f"Wake-on-LAN sent to {WOL_TARGET_MAC} via {WOL_BROADCAST_IP}:{WOL_PORT}"
    except Exception as exc:
        return False, f"Failed to send WoL packet: {exc}"


# ── Startup helper ────────────────────────────────────────────────────────────

def prime_counters() -> None:
    """Call once at startup to initialise rolling counters and probe static info."""
    psutil.cpu_percent(interval=0.1)
    net_speed()
    # Probe the system once so the first request doesn't pay the cost
    get_system_info()


# ── GPU stats (AMD via sysfs) ─────────────────────────────────────────────────

def get_gpu_stats() -> dict | None:
    """Return AMD GPU stats read from sysfs, or None if not available.

    All sysfs discovery is cached in :class:`SystemInfo`; this function only
    reads the live counter files.  Reports:
      - temp    (junction if available, else edge)  °C
      - fan_rpm / fan_pct (from fan1_input / pwm1)
      - power_w (power1_average, µW → W)
      - usage   (gpu_busy_percent, 0–100)
    """
    paths = get_system_info().amd_gpu
    if paths is None:
        return None

    def _read(path: str | None) -> str | None:
        if path is None:
            return None
        try:
            return open(path).read().strip()
        except Exception:
            return None

    temp_raw  = _read(paths.temp_junction) or _read(paths.temp_edge)
    fan_raw   = _read(paths.fan_rpm)
    pwm_raw   = _read(paths.fan_pwm)
    power_raw = _read(paths.power)
    usage_raw = _read(paths.usage)

    # VRAM (bytes → GB)
    vram_total_b = _read(paths.vram_total)
    vram_used_b  = _read(paths.vram_used)
    vram_pct     = None
    if vram_total_b and vram_used_b:
        try:
            vram_pct = round(int(vram_used_b) / int(vram_total_b) * 100, 1)
        except (ValueError, ZeroDivisionError):
            pass

    temp       = round(int(temp_raw) / 1000, 1) if temp_raw else None
    fan_rpm    = int(fan_raw) if fan_raw else None
    fan_pct    = round(int(pwm_raw) / 255.0 * 100, 1) if pwm_raw is not None else None
    power_w    = round(int(power_raw) / 1_000_000, 1) if power_raw else None

    # EMA smoothing for GPU usage (alpha=0.3, window=12 samples ≈ 24s @ 2s poll)
    smoothed_usage: float | int | None = None
    if usage_raw is not None:
        try:
            new_val = int(usage_raw)
        except ValueError:
            pass
        else:
            _window = getattr(get_system_info(), '_gpu_usage_window', None)
            if _window is None:
                # Lazy-init the smoothing window on first use
                _window = list[int]()
                object.__setattr__(get_system_info(), '_gpu_usage_window', _window)
            _window.append(new_val)
            if len(_window) > 12:
                del _window[0]
            smoothed_usage = sum(_window) / len(_window)

    if all(v is None for v in (temp, fan_rpm, fan_pct, power_w)):
        return None

    return {
        "temp":         temp,
        "fan_rpm":      fan_rpm,
        "fan_pct":      fan_pct,
        "power_w":      power_w,
        "usage":        smoothed_usage if smoothed_usage is not None else usage_raw,
        "vram_used_gb": round(int(vram_used_b) / 1024**3, 1) if vram_used_b else None,
        "vram_total_gb": round(int(vram_total_b) / 1024**3, 1) if vram_total_b else None,
        "vram_pct":     vram_pct,
    }


# ── Ollama ─────────────────────────────────────────────────────────────────────

def get_ollama_model() -> list[dict]:
    """Return currently-loaded Ollama models, or [] if unavailable/unconfigured."""
    if not OLLAMA_URL:
        return []
    try:
        r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=3)
        if r.ok:
            return [
                {
                    "name":    m.get("name", ""),
                    "size_gb": round(m.get("size", 0) / 1e9, 1),
                }
                for m in r.json().get("models", [])
            ]
    except Exception:
        pass
    return []


# ── Feature flags ──────────────────────────────────────────────────────────────

def get_features() -> dict:
    """Return which optional integrations are configured via .env."""
    return {
        "wol":     bool(WOL_TARGET_MAC),
        "shelly":  bool(SHELLY_PLUG_URL),
        "shelly2": bool(SHELLY_PLUG_2_URL),
        "adguard": bool(ADGUARD_URL),
        "ollama":  bool(OLLAMA_URL),
    }
