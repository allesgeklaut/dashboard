from __future__ import annotations
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import homelab_core as core

app = FastAPI(title="HOMELAB//CTRL")

# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def api_stats():
    return core.get_system_stats()

@app.get("/api/storage")
def api_storage():
    return core.get_storage()

@app.get("/api/adguard")
def api_adguard():
    return core.get_adguard_stats()

@app.get("/api/containers")
def api_containers():
    containers, _ = core.get_containers()
    return containers

@app.post("/api/containers/{eid}/{cid}/{action}")
def api_action(eid: str, cid: str, action: str):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "invalid action")
    eid_val = int(eid) if eid not in ("null", "none", "") else None
    ok, msg = core.container_action(cid, action, eid_val)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True}


@app.get("/api/shelly")
def api_shelly():
    return core.get_shelly_stats()

@app.post("/api/shelly/powercycle")
def api_shelly_powercycle():
    ok, msg = core.shelly_power_cycle()
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg}

@app.get("/api/shelly2")
def api_shelly2():
    return core.get_shelly2_state()

@app.post("/api/shelly2/toggle")
def api_shelly2_toggle():
    ok, msg = core.shelly2_toggle()
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg}

@app.get("/api/processes")
def api_processes():
    try:
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
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

# prime rolling counters and start background Shelly energy tracking
core.prime_counters()
core.start_energy_tracker()
