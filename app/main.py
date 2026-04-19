from __future__ import annotations
import time, socket, os, threading, subprocess
from datetime import timedelta
from pathlib import Path
import psutil, requests, urllib3
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PORTAINER_URL     = os.getenv("PORTAINER_URL",     "https://192.168.0.43:9443")
PORTAINER_API_KEY = os.getenv("PORTAINER_API_KEY", "")
ADGUARD_URL       = os.getenv("ADGUARD_URL",       "http://192.168.0.2")
ADGUARD_USER      = os.getenv("ADGUARD_USER",      "")
ADGUARD_PASS      = os.getenv("ADGUARD_PASS",      "")
NFS_MOUNTS        = [m.strip() for m in os.getenv("NFS_MOUNTS", "/mnt/nas").split(",") if m.strip()]

_HDR  = {"X-API-Key": PORTAINER_API_KEY}
app   = FastAPI(title="HOMELAB//CTRL")

# ── net speed ─────────────────────────────────────────────────────────────────
_pnet, _ptm, _nlk = None, None, threading.Lock()

def net_speed():
    global _pnet, _ptm
    with _nlk:
        n, now = psutil.net_io_counters(), time.monotonic()
        if _pnet is None:
            _pnet, _ptm = n, now
            return 0.0, 0.0
        dt = (now - _ptm) or 1e-3
        tx = (n.bytes_sent - _pnet.bytes_sent) / dt
        rx = (n.bytes_recv - _pnet.bytes_recv) / dt
        _pnet, _ptm = n, now
        return tx, rx

# ── portainer ─────────────────────────────────────────────────────────────────
_eids, _enames, _elk = None, {}, threading.Lock()

def get_eids():
    global _eids, _enames
    with _elk:
        if _eids is not None:
            return _eids
    try:
        r = requests.get(f"{PORTAINER_URL}/api/endpoints", headers=_HDR, timeout=3, verify=False)
        if r.ok:
            data = r.json()
            with _elk:
                _eids   = [e["Id"] for e in data]
                _enames = {e["Id"]: e["Name"] for e in data}
            return _eids
    except Exception:
        pass
    return []

def portainer_containers():
    out = []
    for eid in get_eids():
        try:
            r = requests.get(
                f"{PORTAINER_URL}/api/endpoints/{eid}/docker/containers/json?all=true",
                headers=_HDR, timeout=4, verify=False
            )
            if r.ok:
                host = _enames.get(eid, str(eid))
                for c in r.json():
                    out.append({
                        "id":     c["Id"][:12],
                        "name":   c["Names"][0].lstrip("/"),
                        "state":  c["State"],
                        "status": c["Status"],
                        "image":  c["Image"].split("/")[-1].split(":")[0],
                        "host":   host,
                        "eid":    eid,
                    })
        except Exception:
            pass
    return out

def cli_containers():
    try:
        raw = subprocess.check_output(
            ["docker", "ps", "-a", "--format",
             "{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}"],
            timeout=4, stderr=subprocess.DEVNULL
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

def container_action(cid: str, action: str, eid):
    if eid is not None:
        try:
            r = requests.post(
                f"{PORTAINER_URL}/api/endpoints/{eid}/docker/containers/{cid}/{action}",
                headers=_HDR, timeout=15, verify=False
            )
            if r.status_code in (200, 204, 304):
                return True, "ok"
            return False, f"HTTP {r.status_code}"
        except Exception as e:
            return False, str(e)
    try:
        subprocess.check_call(["docker", action, cid], timeout=10, stderr=subprocess.DEVNULL)
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def api_stats():
    cpu  = psutil.cpu_percent()
    mem  = psutil.virtual_memory()
    swp  = psutil.swap_memory()
    l1, l5, l15 = psutil.getloadavg()
    tx, rx = net_speed()

    temp = None
    try:
        for key in ("coretemp", "cpu_thermal", "k10temp", "acpitz"):
            s = psutil.sensors_temperatures().get(key)
            if s:
                temp = round(s[0].current, 1)
                break
    except Exception:
        pass

    ip = "n/a"
    for iface, addrs in psutil.net_if_addrs().items():
        if iface == "lo":
            continue
        for a in addrs:
            if a.family == 2 and not a.address.startswith("127."):
                ip = a.address
                break
        if ip != "n/a":
            break

    td = timedelta(seconds=int(time.time() - psutil.boot_time()))
    h, rem = divmod(td.seconds, 3600)
    m = rem // 60

    return {
        "cpu":          cpu,
        "ram_pct":      mem.percent,
        "ram_used_gb":  round(mem.used  / 1024**3, 1),
        "ram_total_gb": round(mem.total / 1024**3, 1),
        "swap_pct":     swp.percent,
        "temp":         temp,
        "load1":        round(l1, 2),
        "load5":        round(l5, 2),
        "load15":       round(l15, 2),
        "net_tx":       tx,
        "net_rx":       rx,
        "uptime":       f"{td.days}d {h:02d}h {m:02d}m",
        "hostname":     socket.gethostname(),
        "ip":           ip,
    }

@app.get("/api/storage")
def api_storage():
    out, seen = [], set()
    for mp in NFS_MOUNTS:
        if not os.path.ismount(mp):
            out.append({"mount": mp, "error": "not mounted", "type": "NFS"})
            seen.add(mp)
            continue
        try:
            u = psutil.disk_usage(mp)
            out.append({"mount": mp, "used_gb": round(u.used/1024**3,1),
                         "total_gb": round(u.total/1024**3,1), "percent": u.percent, "type": "NFS"})
            seen.add(mp)
        except Exception:
            out.append({"mount": mp, "error": "read error", "type": "NFS"})

    cnt = 0
    for p in psutil.disk_partitions(all=False):
        if p.mountpoint in seen or cnt >= 4:
            continue
        if any(x in p.fstype for x in ("tmpfs","devtmpfs","squashfs","overlay","efi")):
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
            if u.total < 1e8:
                continue
            out.append({"mount": p.mountpoint, "used_gb": round(u.used/1024**3,1),
                         "total_gb": round(u.total/1024**3,1), "percent": u.percent, "type": p.fstype})
            seen.add(p.mountpoint)
            cnt += 1
        except Exception:
            continue
    return out

@app.get("/api/adguard")
def api_adguard():
    try:
        r = requests.get(f"{ADGUARD_URL}/control/stats",
                         auth=(ADGUARD_USER, ADGUARD_PASS), timeout=3)
        if r.ok:
            d = r.json()
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

@app.get("/api/containers")
def api_containers():
    data = portainer_containers()
    if not data:
        data = cli_containers()
    return data

@app.post("/api/containers/{eid}/{cid}/{action}")
def api_action(eid: str, cid: str, action: str):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "invalid action")
    eid_val = int(eid) if eid not in ("null", "none", "") else None
    ok, msg = container_action(cid, action, eid_val)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True}

@app.get("/api/processes")
def api_processes():
    try:
        procs = []
        for p in psutil.process_iter(["pid","name","cpu_percent","memory_percent"]):
            try:
                procs.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        procs.sort(key=lambda x: x.get("cpu_percent") or 0, reverse=True)
        return procs[:12]
    except Exception:
        return []

@app.get("/", response_class=HTMLResponse)
def root():
    return (Path(__file__).parent / "static" / "index.html").read_text()

# prime counters
psutil.cpu_percent(interval=0.1)
net_speed()
