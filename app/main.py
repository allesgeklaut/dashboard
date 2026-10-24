from __future__ import annotations
from pathlib import Path
from urllib.parse import urlsplit

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import homelab_core as core

app = FastAPI(title="HOMELAB//CTRL")
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def protect_mutations(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        try:
            origin = urlsplit(request.headers.get("origin", ""))
        except ValueError:
            return JSONResponse({"detail": "invalid request origin"}, status_code=403)
        # Match the browser-facing Host, not the tunnel's internal HTTP scheme.
        # The mandatory custom header also forces cross-origin browsers through
        # a CORS preflight, which this app deliberately does not authorize.
        same_host = (
            origin.scheme in ("http", "https")
            and origin.netloc.lower() == request.headers.get("host", "").lower()
            and not (origin.path or origin.query or origin.fragment)
        )
        if (not same_host
                or request.headers.get("x-homelab-request") != "1"
                or request.headers.get("sec-fetch-site") == "cross-site"):
            return JSONResponse({"detail": "same-origin dashboard request required"}, status_code=403)
    return await call_next(request)

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

@app.get("/api/llama")
def api_llama():
    return core.get_llama()

@app.get("/api/storage")
def api_storage():
    return core.get_storage()

@app.get("/api/adguard")
def api_adguard():
    return core.get_adguard_stats()

@app.get("/api/opencode-go")
def api_opencode_go():
    return core.get_opencode_go_usage()

@app.get("/api/opencode-go-limits")
def api_opencode_go_limits():
    return core.get_opencode_go_limits()

@app.post("/api/opencode-go-benchmarks/fetch")
def api_opencode_go_benchmarks_fetch():
    res = core.fetch_opencode_go_benchmarks()
    if not res.get("ok"):
        raise HTTPException(500, res.get("error", "fetch failed"))
    return res

@app.get("/api/mistral-models")
def api_mistral_models():
    return core.get_mistral_models()

@app.post("/api/benchmarks/fetch")
def api_benchmarks_fetch():
    res = core.fetch_benchmarks()
    if not res.get("ok"):
        raise HTTPException(500, res.get("error", "fetch failed"))
    return res

@app.get("/api/containers")
def api_containers():
    containers, source = core.get_containers()
    if source == "error":
        return {"error": "portainer unavailable"}
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

@app.get("/api/shelly/history")
def api_shelly_history():
    return core.get_shelly_history()

@app.post("/api/shelly/powercycle")
def api_shelly_powercycle():
    ok, msg = core.shelly_power_cycle(core.SHELLY_PLUG_URL, 10)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg}

@app.get("/api/shelly2")
def api_shelly2():
    return core.get_shelly2_state()

@app.post("/api/shelly2/powercycle")
def api_shelly2_powercycle():
    ok, msg = core.shelly_power_cycle(core.SHELLY_PLUG_2_URL, 10)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg}

@app.get("/api/stream")
def api_stream():
    return core.get_stream_status()

@app.post("/api/stream/up")
def api_stream_up():
    ok, msg = core.request_stream("up")
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg}

@app.post("/api/stream/up-1440p")
def api_stream_up_1440p():
    ok, msg = core.request_stream("up-1440p")
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg}

@app.post("/api/stream/down")
def api_stream_down():
    ok, msg = core.request_stream("down")
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg}

@app.post("/api/wake")
def api_wake():
    ok, msg = core.wake_desktop()
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg}

@app.post("/api/desktop/power")
def api_desktop_power():
    ok, msg = core.poweroff_desktop()
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg}

@app.get("/api/desktop")
def api_desktop():
    return core.get_desktop_status()

@app.get("/api/processes")
def api_processes():
    try:
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "memory_info"]):
            try:
                info = p.info
                rss = info.get("memory_info")
                # rss 0 = kernel thread (no userland memory) — hide like top does
                if not rss or rss.rss == 0:
                    continue
                info["rss_mb"] = round(rss.rss / 1024**2, 1)
                del info["memory_info"]
                procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return procs
    except Exception:
        return []


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC_DIR / "icons" / "favicon.ico", media_type="image/vnd.microsoft.icon")


@app.get("/", response_class=HTMLResponse)
def root():
    return (STATIC_DIR / "index.html").read_text()

# prime rolling counters and start background Shelly energy tracking
core.prime_counters()
core.start_energy_tracker()
core.start_desktop_monitor()
