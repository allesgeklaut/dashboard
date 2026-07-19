from __future__ import annotations
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException, Body
import os
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import homelab_core as core

app = FastAPI(title="HOMELAB//CTRL")

# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def api_stats():
    stats = core.get_system_stats()
    stats["gpu"] = core.get_gpu_stats()
    return stats


@app.get("/api/features")
def api_features():
    return core.get_features()


@app.get("/api/ollama")
def api_ollama():
    return core.get_ollama_model()

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
    ok, msg = core.shelly_power_cycle(core.SHELLY_PLUG_URL, 10)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg}

@app.get("/api/wol/config")
def api_wol_config():
    """Return WOL configuration for frontend display."""
    return {
        "target_mac": core.WOL_TARGET_MAC or "",
        "broadcast_ip": core.WOL_BROADCAST_IP,
        "port": core.WOL_PORT,
        "target_ip": os.getenv("WOL_TARGET_IP", "")
    }

@app.get("/api/shelly2")
def api_shelly2():
    return core.get_shelly2_state()

@app.post("/api/shelly2/powercycle")
def api_shelly2_powercycle():
    ok, msg = core.shelly_power_cycle(core.SHELLY_PLUG_2_URL, 10)
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

@app.post("/api/wol/{target_ip}")
def api_wol(target_ip: str):
    """Send Wake-on-LAN packet to wake up a target machine.
    
    hostname is used for display purposes; the actual MAC address comes from WOL_TARGET_MAC config.
    Returns status of the WoL send attempt.
    """
    ok, msg = core.wol_send(None)  # None means use default WOL_TARGET_MAC from homelab_core.py
    if not ok:
        raise HTTPException(500, detail=msg)
    # Give the NIC a moment to wake up before probing.
    import time
    time.sleep(5)
    on, status_msg = core.is_target_on(target_ip)
    return {"ok": True, "msg": msg, "target_on": on, "status_message": status_msg}

@app.post("/api/shutdown/{hostname}")
def api_shutdown(hostname: str, payload: dict = Body(...)):
    # Payload may contain a sudo password for the shutdown command.
    pwd = payload.get('password') if isinstance(payload, dict) else None
    ok, msg = core.remote_shutdown(hostname, pwd)
    return {"ok": ok, "msg": msg}

@app.get("/", response_class=HTMLResponse)
def root():
    return (Path(__file__).parent / "static" / "index.html").read_text()

# prime rolling counters and start background Shelly energy tracking
core.prime_counters()
core.start_energy_tracker()
