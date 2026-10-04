"""
Images and container detail for the manager's Deployment panel.

Talks to the runtime's Docker-compatible API over the mounted socket (podman or
docker), like manager/containers.py. The clean-up plan follows the host's
do_gc in scripts/upgrade.sh — keep the newest `retention_count` ZMM versions
and the rollback image, spare untagged build leftovers younger than
`dangling_min_age_h` — but also never touches an image any container uses
(do_gc protects only the app and -previous), and offers unused non-ZMM images
unticked. Deletion only ever removes what a freshly computed plan lists.
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional

from manager import containers, upgrade

logger = logging.getLogger("manager.images")

DEFAULT_RETENTION = 2          # do_gc's fallback when version.json has none
DEFAULT_DANGLING_AGE_H = 48    # do_gc's: younger untagged layers may be an unfinished build's cache
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASS|KEY|AUTH|CREDENTIAL|PRIVATE|COOKIE|SESSION", re.I)


def _client(sock: str):
    import httpx
    return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=sock),
                             base_url="http://d", timeout=30.0)


def _short(image_id: str) -> str:
    return (image_id or "").removeprefix("sha256:")[:12]


def _tags(img: Dict[str, Any]) -> List[str]:
    return [t for t in (img.get("RepoTags") or []) if t and t != "<none>:<none>"]


async def inventory() -> Dict[str, Any]:
    """Every image on the runtime, what kind it is and which containers use it. Never raises."""
    sock = containers.detect_socket()
    if not sock:
        return {"available": False, "error": "no container socket mounted", "images": []}
    try:
        async with _client(sock) as cx:
            imgs = (await cx.get("/images/json")).json()
            ctrs = (await cx.get("/containers/json", params={"all": "true"})).json()
    except Exception as e:
        logger.warning("image inventory failed: %s", e)
        return {"available": False, "error": str(e), "images": []}

    used: Dict[str, List[str]] = {}
    for c in ctrs:
        name = ((c.get("Names") or ["?"])[0]).lstrip("/")
        used.setdefault(_short(c.get("ImageID") or ""), []).append(name)

    current = upgrade.version_state().get("current_version")
    out = []
    for img in imgs:
        tags = _tags(img)
        zmm = next((m for m in (upgrade._TAG_RE.search(t) for t in tags) if m), None)
        out.append({
            "id": img.get("Id"),
            "short_id": _short(img.get("Id")),
            "tags": tags,
            "kind": "zmm" if zmm else ("dangling" if not tags else "other"),
            "version": zmm.group("ver") if zmm else None,
            "current": bool(zmm and zmm.group("ver") == current),
            "size": int(img.get("Size") or 0),
            "created": int(img.get("Created") or 0),
            "used_by": sorted(used.get(_short(img.get("Id")), [])),
        })
    out.sort(key=lambda i: i["created"], reverse=True)
    return {"available": True, "error": None, "images": out}


def _plan_from(images: List[Dict[str, Any]], state: Dict[str, Any], now: float) -> Dict[str, Any]:
    keep = state.get("retention_count")
    keep = keep if isinstance(keep, int) and keep >= 1 else DEFAULT_RETENTION
    age_h = state.get("dangling_min_age_h")
    age_h = age_h if isinstance(age_h, int) and age_h >= 0 else DEFAULT_DANGLING_AGE_H
    rollback_tag = state.get("previous_image_tag")

    candidates, kept = [], []

    def keep_it(img, why):
        kept.append({**img, "reason": why})

    def offer(img, why, default):
        candidates.append({**img, "reason": why, "default": default})

    zmm = [i for i in images if i["kind"] == "zmm"]   # already newest first
    for rank, img in enumerate(zmm):
        if img["used_by"]:
            keep_it(img, "used by " + ", ".join(img["used_by"]))
        elif img["current"]:
            keep_it(img, "the running version")
        elif rollback_tag and rollback_tag in img["tags"]:
            keep_it(img, "the rollback image")
        elif rank < keep:
            keep_it(img, f"one of the newest {keep} versions kept")
        else:
            offer(img, f"older version beyond the newest {keep} kept", True)

    for img in images:
        if img["kind"] == "zmm":
            continue
        if img["used_by"]:
            keep_it(img, "used by " + ", ".join(img["used_by"]))
        elif img["kind"] == "dangling":
            age = (now - img["created"]) / 3600
            if age < age_h:
                keep_it(img, f"untagged, under {age_h}h old — may be an unfinished build's cache")
            else:
                offer(img, f"untagged build leftover, {int(age // 24)} days old", True)
        else:
            offer(img, "not used by any container", False)

    return {"retention_count": keep, "dangling_min_age_h": age_h,
            "candidates": candidates, "kept": kept,
            # Images share layers, so the real saving can be lower.
            "default_bytes": sum(c["size"] for c in candidates if c["default"])}


async def cleanup_plan() -> Dict[str, Any]:
    inv = await inventory()
    if not inv["available"]:
        return {"available": False, "error": inv["error"], "candidates": [], "kept": []}
    plan = _plan_from(inv["images"], upgrade.version_state(), time.time())
    return {"available": True, "error": None, **plan}


async def cleanup(ids: List[str]) -> Dict[str, Any]:
    """Remove the requested images, each only if a fresh plan still lists it."""
    plan = await cleanup_plan()
    if not plan["available"]:
        return {"success": False, "error": plan["error"], "results": []}
    allowed = {c["id"]: c for c in plan["candidates"]}
    sock = containers.detect_socket()
    results, freed = [], 0
    async with _client(sock) as cx:
        for image_id in dict.fromkeys(ids or []):
            img = allowed.get(image_id)
            if not img:
                results.append({"id": image_id, "ok": False, "error": "not in the current clean-up plan"})
                continue
            # Untag one name at a time: removing an ID that has several names needs
            # force, and force would also remove an image a container just started using.
            refs = img["tags"] or [image_id]
            error = None
            for ref in refs:
                r = await cx.delete(f"/images/{ref}")
                if r.status_code not in (200, 204):
                    error = f"{r.status_code}: {r.text[:200]}"
                    break
            if error:
                results.append({"id": image_id, "ok": False, "error": error})
            else:
                freed += img["size"]
                results.append({"id": image_id, "ok": True})
    ok = sum(r["ok"] for r in results)
    return {"success": ok == len(results), "removed": ok, "failed": len(results) - ok,
            "freed_bytes": freed, "results": results}


def _cpu_percent(st: Dict[str, Any]) -> Optional[float]:
    cpu, pre = st.get("cpu_stats") or {}, st.get("precpu_stats") or {}
    try:
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
    except (KeyError, TypeError):
        return None
    cpus = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    if sys_delta <= 0 or cpu_delta < 0:
        return None
    return round(cpu_delta / sys_delta * cpus * 100, 1)


async def container_detail(name: str) -> Optional[Dict[str, Any]]:
    """Config, health and resource use of one visible container; None if not visible/found."""
    if not containers.visible_container(name):
        return None
    sock = containers.detect_socket()
    if not sock:
        return None
    try:
        async with _client(sock) as cx:
            r = await cx.get(f"/containers/{name}/json")
            if r.status_code != 200:
                return None
            info = r.json()
            stats = None
            if (info.get("State") or {}).get("Running"):
                s = await cx.get(f"/containers/{name}/stats", params={"stream": "false"})
                stats = s.json() if s.status_code == 200 else None
    except Exception as e:
        logger.warning("container_detail(%s) failed: %s", name, e)
        return None

    cfg, hc, st = info.get("Config") or {}, info.get("HostConfig") or {}, info.get("State") or {}
    env = []
    for item in cfg.get("Env") or []:
        k, _, v = item.partition("=")
        env.append({"name": k, "value": "••••••" if SECRET_ENV.search(k) else v,
                    "masked": bool(SECRET_ENV.search(k))})
    ports = []
    for cport, binds in (hc.get("PortBindings") or {}).items():
        for b in binds or []:
            ports.append(f"{b.get('HostIp') or '0.0.0.0'}:{b.get('HostPort')} → {cport}")
    health = (st.get("Health") or st.get("Healthcheck") or {}).get("Status")
    mem = (stats or {}).get("memory_stats") or {}
    restart = hc.get("RestartPolicy") or {}
    return {
        "name": name,
        "image": cfg.get("Image"),
        "image_id": _short(info.get("Image")),
        "created": info.get("Created"),
        "started_at": st.get("StartedAt"),
        "state": st.get("Status"),
        "health": health,
        "restart_policy": restart.get("Name") or "no",
        "restart_count": info.get("RestartCount"),
        "network_mode": hc.get("NetworkMode"),
        "ports": ports,
        "mounts": [{"source": m.get("Source"), "destination": m.get("Destination"),
                    "rw": bool(m.get("RW")), "type": m.get("Type")} for m in info.get("Mounts") or []],
        "devices": [f"{d.get('PathOnHost')} → {d.get('PathInContainer')}" for d in hc.get("Devices") or []],
        "command": " ".join((cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or [])),
        "env": env,
        "cpu_percent": _cpu_percent(stats) if stats else None,
        "memory_bytes": mem.get("usage"),
        "memory_limit_bytes": mem.get("limit"),
    }
