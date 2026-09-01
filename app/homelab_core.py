"""
homelab_core.py — shared data-fetching helpers for HOMELAB//CTRL
Used by both main.py (FastAPI backend) and dashboard.py (Textual TUI).
"""
from __future__ import annotations
import os, socket, threading, time, json
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
LLAMA_SERVER_URL = os.getenv("LLAMA_SERVER_URL")          # None if not set → feature disabled
STREAM_SPOOL_DIR = os.getenv("STREAM_SPOOL_DIR")  # None if not set → feature disabled

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
    fan_rpm:       str | None = None   # fan1_input (sysfs path)
    fan_max:       int | None = None   # fan1_max value (resolved once, stored as int)
    fan_pwm:       str | None = None   # pwm1 (sysfs path)
    power:         str | None = None   # power1_average (µW)
    power_inst:    str | None = None   # power1_instantaneous (µW)
    usage:         str | None = None   # gpu_busy_percent
    vram_total:    str | None = None   # mem_info_vram_total (bytes)
    vram_used:     str | None = None   # mem_info_vram_used  (bytes)

    def live_paths(self) -> list[str]:
        """Sysfs files that the live getters will actually read."""
        return [p for p in (
            self.temp_junction, self.temp_edge, self.fan_rpm,
            self.fan_pwm, self.power, self.power_inst, self.usage,
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
        # The web container runs on a docker bridge, so its own interfaces are
        # the container's (192.168.144.x), not the host's.  HOST_IP is set in
        # docker-compose.yml (host networking was dropped for security); only
        # fall back to interface probing when unset (dev outside compose).
        env_ip = os.environ.get("HOST_IP", "").strip()
        if env_ip:
            return env_ip
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

        # Read fan1_max once — it's a static value, no need to re-read every poll.
        fan1_max_val: int | None = None
        for cand in (f"{hwmon_path}/fan1_max", f"{card_path}/device/hwmon/fan1_max"):
            try:
                fan1_max_val = int(open(cand).read().strip())
                break
            except Exception:
                continue

        paths = _GpuPaths(
            hwmon_path    = hwmon_path,
            card_path     = card_path,
            temp_junction = _exists(f"{hwmon_path}/temp2_input"),
            temp_edge     = _exists(f"{hwmon_path}/temp1_input"),
            fan_rpm       = _exists(f"{hwmon_path}/fan1_input"),
            fan_max       = fan1_max_val,
            fan_pwm       = _exists(f"{hwmon_path}/pwm1"),
            power         = _exists(f"{hwmon_path}/power1_average"),
            power_inst    = _exists(f"{hwmon_path}/power1_instantaneous"),
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

def get_containers() -> tuple[list[dict], str]:
    """Return (containers, source) where source is 'portainer' or 'none'."""
    containers = portainer_containers()
    if containers:
        return containers, "portainer"
    return [], "none"

# ── Container actions ─────────────────────────────────────────────────────────

def container_action(cid: str, action: str, eid: int | None = None) -> tuple[bool, str]:
    """Start, stop, or restart a container via Portainer."""
    if eid is None:
        eids = get_eids()
        eid  = eids[0] if eids else None
    if eid is None:
        return False, "no portainer endpoint"
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
    temp = get_temp()
    td   = timedelta(seconds=int(time.time() - info.boot_time))
    h, rem = divmod(td.seconds, 3600)
    m = rem // 60
    return {
        "cpu":          cpu,
        "cores":        psutil.cpu_count() or 1,
        "ram_pct":      mem.percent,
        "ram_used_gb":  round(mem.used  / 1024**3, 1),
        "ram_total_gb": round(mem.total / 1024**3, 1),
        "swap_pct":     swp.percent,
        "temp":         round(temp, 1) if temp is not None else None,
        "load1":        round(l1,  2),
        "load5":        round(l5,  2),
        "load15":       round(l15, 2),
        "uptime":       f"{td.days}d {h:02d}h {m:02d}m",
        "hostname":     os.environ.get("HOST_NAME", "").strip() or socket.gethostname(),
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
    "yesterday":       "",
    "yesterday_wh":    0.0,
    "_last_minute_ts": 0,     # unix ts of last processed minute
    "today_history":   [],    # [[minute_ts, wh], ...] per-minute energy samples
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
        saved.setdefault("today_history", [])
        # Drop history samples that don't belong to the stored day (safety net
        # against corrupted or hand-edited files).
        day = saved.get("today", "")
        day_start = 0
        if day:
            try:
                day_start = _dt.datetime.fromisoformat(day).timestamp()
            except ValueError:
                day = ""
        if day:
            saved["today_history"] = [
                s for s in saved["today_history"]
                if isinstance(s, (list, tuple)) and len(s) == 2
                and isinstance(s[0], (int, float)) and s[0] >= day_start
            ]
        else:
            saved["today_history"] = []
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
    """Add newly seen per-minute mWh values to today's Wh accumulator and
    append them as history samples."""
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
        _energy_data["today_history"] = []

    # by_minute[0] = most recent complete minute, [1] = one before, etc.
    fresh = [float(v) for v in by_minute[:new_mins]]
    added_wh = sum(fresh) / 1000.0   # mWh → Wh
    _energy_data["today_wh"]        = round(_energy_data["today_wh"] + added_wh, 3)
    _energy_data["_last_minute_ts"] = minute_ts

    # Append chronologically (oldest first): fresh = [newest, ..., oldest].
    hist = _energy_data["today_history"]
    for ts, wh in zip(
        range(minute_ts - (len(fresh) - 1) * 60, minute_ts + 1, 60),
        reversed(fresh),
    ):
        hist.append([ts, round(wh / 1000.0, 5)])   # mWh → Wh
    # Cap at one full day of samples (safety net; rollover clears it anyway).
    if len(hist) > 1440:
        del hist[:len(hist) - 1440]


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



def get_shelly_history() -> dict:
    """Return today's per-minute energy samples, converted to average watts.

    The tracker stores [minute_ts, wh_per_minute]; watt = wh * 60.
    """
    samples = [
        [int(ts), round(wh * 60.0, 1)]
        for ts, wh in _energy_data.get("today_history", [])
    ]
    return {
        "date":   _energy_data.get("today", ""),
        "samples": samples,
    }


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
      - power_w (power1_instantaneous, else power1_average; µW → W)
      - usage   (gpu_busy_percent, 3-sample rolling mean, 0–100)
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
    # power1_instantaneous is the latest driver sample (no moving average);
    # power1_average is the driver's sliding-window mean — fall back to it
    # when the instantaneous file isn't exposed by the firmware/driver.
    power_raw = _read(paths.power_inst) or _read(paths.power)
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
    
    # Calculate fan percentage using actual RPM when available (more accurate than PWM duty cycle)
    if paths.fan_max and fan_raw:
        try:
            fan_pct = round(int(fan_raw) / paths.fan_max * 100, 1)
        except ValueError:
            fan_pct = None
    elif pwm_raw is not None:
        fan_pct = round(int(pwm_raw) / 255.0 * 100, 1)
    else:
        fan_pct = None
        
    power_w    = round(int(power_raw) / 1_000_000, 1) if power_raw else None

    # gpu_busy_percent is a ~1 s driver-side window that still dips for
    # ~2.5 s even under sustained llama decode — a 3-sample window
    # (≈6 s @ 2 s poll) absorbs those dips without the old 12-sample
    # (≈24 s) sluggishness.
    usage: float | int | None = None
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
            if len(_window) > 3:
                del _window[0]
            usage = sum(_window) / len(_window)

    if all(v is None for v in (temp, fan_rpm, fan_pct, power_w)):
        return None

    return {
        "temp":         temp,
        "fan_rpm":      fan_rpm,
        "fan_pct":      fan_pct,
        "power_w":      power_w,
        "usage":        usage,
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


# ── llama.cpp server (llama-server) ─────────────────────────────────────────────
# Live view of what the local LLM is doing. Two plain-HTTP sources (no docker
# socket needed):
#   /metrics — Prometheus counters/gauges. Updated only at request *completion*,
#              so it carries lifetime totals, lifetime averages and the
#              speculative-decode stats, plus a delta fallback for short
#              requests that finish between two polls.
#   /slots   — per-slot progress (n_decoded, n_prompt_tokens_processed) that
#              updates in real time; source of live tok/s while generating.

_LLM_TRACKED = (
    "llamacpp:tokens_predicted_total", "llamacpp:tokens_predicted_seconds_total",
    "llamacpp:prompt_tokens_total", "llamacpp:prompt_seconds_total",
)
_LLM_LAST: dict[str, float] = {}
_LLM_SLOT = {"id_task": None, "t": 0.0, "n_decoded": 0, "n_prompt": 0}
_LLM_AVG: dict[str, float] = {}  # *_tokens_seconds gauges read 0 mid-request
_LLM_MIN: dict[str, float] = {}  # lowest seen *_total counters — restart baseline

def _llm_restarted(m: dict[str, float]) -> bool:
    """Return True when a cumulative *_total counter dropped below the lowest
    value we've seen.  The totals only ever increase while llama-server runs, so
    a drop means the process restarted and reset its counters to 0.  On restart
    the cached average/slot state from the old process must be discarded."""
    for k in _LLM_TRACKED:
        v = m.get(k)
        if v is None:
            continue
        if v < _LLM_MIN.get(k, v):
            _LLM_MIN.clear()
            for _k in _LLM_TRACKED:
                _v = m.get(_k)
                if _v is not None:
                    _LLM_MIN[_k] = _v
            return True
        _LLM_MIN.setdefault(k, v)
    return False

def _parse_prometheus(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line[0] in "# \t":
            continue
        name, sep, value = line.rpartition("}")
        if sep:
            name = name.rsplit(" ", 1)[0]
        else:
            name, _, value = line.rpartition(" ")
        try:
            out[name] = float(value.strip())
        except ValueError:
            continue
    return out

def _live_rate(prev: dict[str, float], cur: dict[str, float], count: str, seconds: str) -> float | None:
    """Counter delta between scrapes → tokens/s since the last poll."""
    if count not in cur or seconds not in cur:
        return None
    d_count = cur[count]  - prev.get(count, 0)
    d_sec   = cur[seconds] - prev.get(seconds, 0)
    if prev and d_count > 0 and d_sec > 0:
        return d_count / d_sec
    return None

def _slot_rates(slots: list, now: float) -> tuple[float | None, float | None, str]:
    """Generation rate from the first processing slot → (gen_tps, prompt_tps, phase).

    Only generation is taken live from the slot: n_decoded advances once per
    decode step (~60 ms), so a 2 s poll sees many steps and the delta is a real
    rate.  n_prompt_tokens_processed however advances in whole llama_decode
    batches (n_batch = 2048 tokens ≈ 10 s at 200 tok/s), so prompt deltas are
    0 or ~2048 — dividing by the 2 s poll interval yields spikes of ~1000 t/s.
    Prompt rate must therefore come from the server-timed /metrics counters
    (handled by the caller's fallback), never from slot deltas.
    """
    act = next((s for s in slots if s.get("is_processing")), None)
    if act is None:
        _LLM_SLOT["id_task"] = None
        return None, None, ""
    task = act.get("id_task")
    nt   = act.get("next_token")
    ntd  = nt if isinstance(nt, dict) else (nt[0] if isinstance(nt, list) and nt else {})
    try:
        n_dec = int(ntd.get("n_decoded", 0) or 0)
    except (TypeError, ValueError):
        n_dec = 0
    n_pr  = act.get("n_prompt_tokens_processed", 0) or 0
    gen_tps = prompt_tps = None
    if _LLM_SLOT["id_task"] == task:        # same request → delta is the live rate
        dt = now - _LLM_SLOT["t"]
        if dt > 0.2:
            if n_dec > _LLM_SLOT["n_decoded"]:
                gen_tps = (n_dec - _LLM_SLOT["n_decoded"]) / dt
    _LLM_SLOT.update(id_task=task, t=now, n_decoded=n_dec, n_prompt=n_pr)
    return gen_tps, prompt_tps, ("generation" if n_dec > 0 else "prompt")

def get_llama() -> dict:
    """Scrape llama-server /metrics + /slots and report the latest activity.

    Returns ``{"url", "running", ...}`` or ``{"error": ...}``.
    """
    if not LLAMA_SERVER_URL:
        return {"error": "LLAMA_SERVER_URL not configured"}
    base = LLAMA_SERVER_URL.rstrip("/")
    try:
        r = requests.get(f"{base}/metrics", timeout=3)
        r.raise_for_status()
        m = _parse_prometheus(r.text)
        slots = requests.get(f"{base}/slots", timeout=3).json()
        if not isinstance(slots, list):
            slots = []
    except Exception as exc:
        _LLM_LAST.clear()
        return {"error": f"llama-server unreachable at {base}: {exc}"}

    prev = dict(_LLM_LAST)
    _LLM_LAST.update({k: m[k] for k in _LLM_TRACKED if k in m})

    # If llama-server restarted, its counters reset to 0 — drop the old
    # process's cached averages and slot deltas so we don't surface stale data.
    if _llm_restarted(m):
        _LLM_AVG.clear()
        _LLM_SLOT.update(id_task=None, t=0.0, n_decoded=0, n_prompt=0)

    now = time.monotonic()
    gen_tps, _, slot_phase = _slot_rates(slots, now)

    # /slots misses requests shorter than one poll — fall back to /metrics
    # counter deltas, which catch a completed request's burst (both counters
    # jump together at completion, so the delta is that request's true rate).
    # This is also the only honest prompt-rate source: the slot's
    # n_prompt_tokens_processed advances in whole decode batches (n_batch =
    # 2048), so a 2 s poll against it yields 0 / ~2048 → ~1000 t/s spikes.
    if gen_tps is None:
        gen_tps = _live_rate(prev, m, "llamacpp:tokens_predicted_total", "llamacpp:tokens_predicted_seconds_total")
    # prompt rate: server-timed counter delta across the scrape window — matches
    # the "prompt eval" log lines (d_count/d_sec == request's own average)
    prompt_tps = _live_rate(prev, m, "llamacpp:prompt_tokens_total", "llamacpp:prompt_seconds_total")

    d_gen    = m.get("llamacpp:tokens_predicted_total", 0) - prev.get("llamacpp:tokens_predicted_total", 0)
    d_prompt = m.get("llamacpp:prompt_tokens_total", 0) - prev.get("llamacpp:prompt_tokens_total", 0)
    if not prev:  # first scrape: no delta baseline yet
        d_gen = d_prompt = 0
    if slot_phase:
        phase = slot_phase
    elif d_gen > 0:
        phase = "generation"
    elif d_prompt > 0:
        phase = "prompt"
    elif m.get("llamacpp:requests_processing", 0) > 0:
        phase = "processing"
    else:
        phase = "idle"

    # the *_tokens_seconds gauges reset to 0 while a request is in flight —
    # surface the last real value instead so the average doesn't flicker
    gen_avg    = m.get("llamacpp:predicted_tokens_seconds")
    prompt_avg = m.get("llamacpp:prompt_tokens_seconds")
    if gen_avg:
        _LLM_AVG["gen"] = gen_avg
    if prompt_avg:
        _LLM_AVG["prompt"] = prompt_avg

    draft    = m.get("llamacpp:spec_decode_num_draft_tokens_total", 0)
    accepted = m.get("llamacpp:spec_decode_num_accepted_tokens_total", 0)
    return {
        "url":               base,
        "running":           True,
        "phase":             phase,
        "gen_tps":           gen_tps,
        "gen_tps_avg":       gen_avg or _LLM_AVG.get("gen"),
        "prompt_tps":        prompt_tps,
        "prompt_tps_avg":    prompt_avg or _LLM_AVG.get("prompt"),
        "tokens_predicted":  int(m.get("llamacpp:tokens_predicted_total", 0)),
        "prompt_tokens":     int(m.get("llamacpp:prompt_tokens_total", 0)),
        "requests_processing": int(m.get("llamacpp:requests_processing", 0)),
        "requests_deferred":   int(m.get("llamacpp:requests_deferred", 0)),
        "draft_accept":      (accepted / draft) if draft > 0 else None,
        "draft_tokens":      int(draft),
    }


# ── Steam streaming control ───────────────────────────────────────────────────
# The container is unprivileged, so up/down *actions* are queued as spool files
# that a host-side systemd .path unit picks up and executes (see host/ in repo
# root).  Status, however, needs no privileges at all: docker-compose already
# uses pid: host so psutil sees the host's compositor / steam processes.

_STREAM_GRAPHICAL_PROCS = ("cosmic-session", "cosmic-comp")
_STREAM_STEAM_PROCS     = ("steam",)

def _proc_running(names: tuple[str, ...]) -> bool:
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] in names:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False

def get_stream_status() -> dict:
    """Headless / ready (graphical, no steam) / streaming (steam up)."""
    if not STREAM_SPOOL_DIR:
        return {"error": "STREAM_SPOOL_DIR not configured"}
    try:
        if _proc_running(_STREAM_STEAM_PROCS):
            state = "streaming"
        elif _proc_running(_STREAM_GRAPHICAL_PROCS):
            state = "ready"
        else:
            state = "headless"
    except Exception as exc:
        return {"error": str(exc)}
    return {"state": state}

def request_stream(action: str) -> tuple[bool, str]:
    """Queue 'up'/'down' for the host-side handler by dropping a spool file."""
    if action not in ("up", "down"):
        return False, "invalid action"
    if not STREAM_SPOOL_DIR:
        return False, "STREAM_SPOOL_DIR not configured"
    try:
        os.makedirs(STREAM_SPOOL_DIR, exist_ok=True)
        uid = f"{os.getpid()}.{threading.get_ident()}"
        tmp  = os.path.join(STREAM_SPOOL_DIR, f".{action}.{uid}.tmp")
        with open(tmp, "w") as f:
            f.write(action)
        # atomic publish so the .path glob never sees a half-written file
        os.replace(tmp, os.path.join(STREAM_SPOOL_DIR, f"{action}.{uid}.request"))
    except Exception as exc:
        return False, str(exc)
    return True, "queued"


# ── Feature flags ──────────────────────────────────────────────────────────────

def get_features() -> dict:
    """Return which optional integrations are configured via .env."""
    return {
        "shelly":  bool(SHELLY_PLUG_URL),
        "shelly2": bool(SHELLY_PLUG_2_URL),
        "adguard": bool(ADGUARD_URL),
        "ollama":  bool(OLLAMA_URL),
        "llama":   bool(LLAMA_SERVER_URL),
        "stream":  bool(STREAM_SPOOL_DIR),
    }
