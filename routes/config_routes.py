"""
Configuration management routes.
Extracted from main.py.
"""
import logging
import os
import yaml
from fastapi import FastAPI
from models import ConfigUpdateRequest
from modules.network_init import (
    generate_pan_id, generate_extended_pan_id,
    generate_network_key, select_best_channel, ZIGBEE_CHANNELS
)
from modules.spectrum_monitor import get_history, get_channel_averages, get_channel_stats, save_scan

logger = logging.getLogger("routes.config")


def _is_auto_local_provider(p: dict) -> bool:
    if not isinstance(p, dict):
        return False
    if p.get("type") != "advanced":
        return False
    # The auto-injected one always carries this exact warning prefix
    warn = (p.get("warning") or "").strip()
    return warn.startswith("I understand I can *destroy* my devices")


def _strip_auto_providers(providers):
    """Remove the auto-injected local 'advanced' provider from a list."""
    if not isinstance(providers, list):
        return []
    return [p for p in providers if not _is_auto_local_provider(p)]


def register_config_routes(app: FastAPI, get_zigbee_service):
    """Register configuration management routes."""

    @app.get("/api/config/structured")
    async def get_structured_config():
        """Return config as structured JSON for the rich settings UI."""
        try:
            with open("./config/config.yaml", "r") as f:
                cfg = yaml.safe_load(f) or {}

            zigbee = cfg.get("zigbee", {})

            def key_to_hex(k):
                if isinstance(k, list):
                    return "".join(f"{b:02X}" for b in k)
                return str(k) if k else ""

            def epan_to_hex(v):
                if isinstance(v, list):
                    return "".join(f"{b:02X}" for b in v)
                return str(v) if v else ""

            ota = cfg.get("ota", {}) or {}

            return {
                "success": True,
                "config": {
                    "zigbee": {
                        "port": zigbee.get("port", ""),
                        "radio_type": zigbee.get("radio_type", "auto"),
                        "channel": zigbee.get("channel", 15),
                        "pan_id": zigbee.get("pan_id", ""),
                        "extended_pan_id_hex": epan_to_hex(zigbee.get("extended_pan_id")),
                        "network_key_hex": key_to_hex(zigbee.get("network_key")),
                        "topology_scan_interval": zigbee.get("topology_scan_interval", 120),
                        "coordinator_type": zigbee.get("coordinator_type", ""),
                    },
                    "mqtt": cfg.get("mqtt", {}),
                    "homeassistant": {
                        "enabled": bool((cfg.get("homeassistant") or {})
                                        .get("enabled", True)),
                    },
                    "web": {k: v for k, v in cfg.get("web", {}).items() if k != "ssl"},
                    "web_ssl": cfg.get("web", {}).get("ssl", {}),
                    "logging": cfg.get("logging", {}),
                    "weather": cfg.get("weather", {}),
                    "media": cfg.get("media", {}),
                    "security": cfg.get("security", {}),
                    "ota": {
                        "enabled": bool(ota.get("enabled", True)),
                        # `providers` is the explicit override list (rarely used).
                        # When unset, zigpy falls back to its own default set.
                        "providers": ota.get("providers", None),
                        # `extra_providers` is the additive list, which the UI
                        # primarily edits. We strip the auto-injected local
                        # advanced provider so the user only sees ones they
                        # actually configured.
                        "extra_providers": _strip_auto_providers(
                            ota.get("extra_providers", [])
                        ),
                        "disable_default_providers": ota.get(
                            "disable_default_providers", []
                        ),
                    },
                }
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/config/structured")
    async def save_structured_config(data: dict):
        """Save structured config back to YAML."""
        try:
            with open("./config/config.yaml", "r") as f:
                cfg = yaml.safe_load(f) or {}

            incoming = data.get("config", data)

            if "mqtt" in incoming:
                cfg.setdefault("mqtt", {}).update(incoming["mqtt"])
            if "homeassistant" in incoming:
                ha = incoming["homeassistant"] or {}
                if "enabled" in ha:
                    cfg.setdefault("homeassistant", {})["enabled"] = bool(ha["enabled"])
            if "web" in incoming:
                cfg.setdefault("web", {}).update(incoming["web"])
            if "web_ssl" in incoming:
                cfg.setdefault("web", {}).setdefault("ssl", {}).update(incoming["web_ssl"])
            if "logging" in incoming:
                cfg.setdefault("logging", {}).update(incoming["logging"])

            if "weather" in incoming:
                w = incoming["weather"]
                weather_cfg = cfg.setdefault("weather", {})
                if "enabled" in w:
                    weather_cfg["enabled"] = bool(w["enabled"])
                if w.get("latitude") is not None:
                    weather_cfg["latitude"] = float(w["latitude"])
                if w.get("longitude") is not None:
                    weather_cfg["longitude"] = float(w["longitude"])
                if w.get("poll_interval_minutes"):
                    weather_cfg["poll_interval_minutes"] = int(w["poll_interval_minutes"])
                if "mqtt_publish" in w:
                    weather_cfg["mqtt_publish"] = bool(w["mqtt_publish"])

            if "media" in incoming:
                m = incoming["media"] or {}
                media_cfg = cfg.setdefault("media", {})
                if "enabled" in m:
                    media_cfg["enabled"] = bool(m["enabled"])
                if m.get("poll_interval_seconds"):
                    media_cfg["poll_interval_seconds"] = int(m["poll_interval_seconds"])
                if "adopt_sessions" in m:
                    media_cfg["adopt_sessions"] = bool(m["adopt_sessions"])
                if "cast" in m:
                    cast_in = m["cast"] or {}
                    cast_cfg = media_cfg.setdefault("cast", {})
                    if "enabled" in cast_in:
                        cast_cfg["enabled"] = bool(cast_in["enabled"])
                    if cast_in.get("app_id"):
                        cast_cfg["app_id"] = str(cast_in["app_id"]).strip()
                    # Custom lyrics receiver (blank = off). Allow clearing it.
                    if "lyrics_app_id" in cast_in:
                        cast_cfg["lyrics_app_id"] = str(cast_in.get("lyrics_app_id") or "").strip()
                    if "karaoke" in cast_in:
                        cast_cfg["karaoke"] = bool(cast_in["karaoke"])
                    if "sync" in cast_in:
                        # Speaker-sync PoC (Settings → Speakers tab saves only
                        # this slice, so merge key-wise like everything else).
                        sync_in = cast_in["sync"] or {}
                        sync_cfg = cast_cfg.setdefault("sync", {})
                        if "enabled" in sync_in:
                            sync_cfg["enabled"] = bool(sync_in["enabled"])
                        if sync_in.get("http_port"):
                            sync_cfg["http_port"] = int(sync_in["http_port"])
                        if "app_id" in sync_in:
                            sync_cfg["app_id"] = str(sync_in.get("app_id") or "").strip()
                if "tts" in m:
                    tts_in = m["tts"] or {}
                    tts_cfg = media_cfg.setdefault("tts", {})
                    if tts_in.get("base_url"):
                        tts_cfg["base_url"] = str(tts_in["base_url"]).strip()
                    if tts_in.get("lang"):
                        tts_cfg["lang"] = str(tts_in["lang"]).strip()
                if "wiim" in m:
                    wiim_in = m["wiim"] or {}
                    wiim_cfg = media_cfg.setdefault("wiim", {})
                    if "enabled" in wiim_in:
                        wiim_cfg["enabled"] = bool(wiim_in["enabled"])
                    if "devices" in wiim_in:
                        # Normalise to a clean list of non-empty IP strings.
                        wiim_cfg["devices"] = [
                            str(d).strip() for d in (wiim_in["devices"] or [])
                            if str(d).strip()
                        ]
                if "radio_browser" in m:
                    rb_in = m["radio_browser"] or {}
                    rb_cfg = media_cfg.setdefault("radio_browser", {})
                    if "enabled" in rb_in:
                        rb_cfg["enabled"] = bool(rb_in["enabled"])
                if "tidal" in m:
                    td_in = m["tidal"] or {}
                    td_cfg = media_cfg.setdefault("tidal", {})
                    if "enabled" in td_in:
                        td_cfg["enabled"] = bool(td_in["enabled"])
                    if td_in.get("quality"):
                        td_cfg["quality"] = str(td_in["quality"]).strip()
                    # Allow clearing the manifest URL (lossless off).
                    if "manifest_base_url" in td_in:
                        td_cfg["manifest_base_url"] = str(td_in.get("manifest_base_url") or "").strip()

            # ---- Security providers (Nuki first; future providers merge
            # their own sub-dict the same way) ----
            if "security" in incoming:
                sec_in = incoming["security"] or {}
                sec_cfg = cfg.setdefault("security", {})
                if "nuki" in sec_in:
                    n = sec_in["nuki"] or {}
                    nuki_cfg = sec_cfg.setdefault("nuki", {})
                    if "enabled" in n:
                        nuki_cfg["enabled"] = bool(n["enabled"])
                    if "bridge" in n:
                        b_in = n["bridge"] or {}
                        b_cfg = nuki_cfg.setdefault("bridge", {})
                        if "enabled" in b_in:
                            b_cfg["enabled"] = bool(b_in["enabled"])
                        if "host" in b_in:
                            b_cfg["host"] = str(b_in.get("host") or "").strip()
                        if b_in.get("port"):
                            b_cfg["port"] = int(b_in["port"])
                        # Blank token = keep the stored one (mqtt-password rule)
                        if b_in.get("token"):
                            b_cfg["token"] = str(b_in["token"]).strip()
                        if "hashed_token" in b_in:
                            b_cfg["hashed_token"] = bool(b_in["hashed_token"])
                    if "matter" in n:
                        m_in = n["matter"] or {}
                        m_cfg = nuki_cfg.setdefault("matter", {})
                        if "enabled" in m_in:
                            m_cfg["enabled"] = bool(m_in["enabled"])
                if "yale" in sec_in:
                    y = sec_in["yale"] or {}
                    yale_cfg = sec_cfg.setdefault("yale", {})
                    if "enabled" in y:
                        yale_cfg["enabled"] = bool(y["enabled"])
                    if "matter" in y:
                        m_in = y["matter"] or {}
                        m_cfg = yale_cfg.setdefault("matter", {})
                        if "enabled" in m_in:
                            m_cfg["enabled"] = bool(m_in["enabled"])

            if "zigbee" in incoming:
                z = incoming["zigbee"]
                zigbee_cfg = cfg.setdefault("zigbee", {})

                for simple_key in ("port", "radio_type", "channel", "topology_scan_interval", "coordinator_type"):
                    if simple_key in z and z[simple_key] != "" and z[simple_key] is not None:
                        zigbee_cfg[simple_key] = z[simple_key]

                if z.get("pan_id"):
                    zigbee_cfg["pan_id"] = z["pan_id"]

                if z.get("extended_pan_id_hex"):
                    h = z["extended_pan_id_hex"].replace(" ", "").replace(":", "")
                    zigbee_cfg["extended_pan_id"] = [int(h[i:i+2], 16) for i in range(0, len(h), 2)]

                if z.get("network_key_hex"):
                    h = z["network_key_hex"].replace(" ", "").replace(":", "")
                    zigbee_cfg["network_key"] = [int(h[i:i+2], 16) for i in range(0, len(h), 2)]

            # ---- OTA ----
            if "ota" in incoming:
                o = incoming["ota"] or {}
                ota_cfg = cfg.setdefault("ota", {})

                if "enabled" in o:
                    ota_cfg["enabled"] = bool(o["enabled"])

                if "extra_providers" in o:
                    cleaned = _strip_auto_providers(o["extra_providers"] or [])
                    if cleaned:
                        ota_cfg["extra_providers"] = cleaned
                    else:
                        # Empty list -> remove the key entirely so the YAML
                        # stays tidy and zigpy uses pure defaults.
                        ota_cfg.pop("extra_providers", None)

                if "providers" in o:
                    if o["providers"]:
                        ota_cfg["providers"] = o["providers"]
                    else:
                        ota_cfg.pop("providers", None)

                if "disable_default_providers" in o:
                    if o["disable_default_providers"]:
                        ota_cfg["disable_default_providers"] = o["disable_default_providers"]
                    else:
                        ota_cfg.pop("disable_default_providers", None)

            with open("./config/config.yaml", "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

            logger.info("Structured config saved via API")
            return {"success": True}
        except Exception as e:
            logger.error(f"Failed to save structured config: {e}")
            return {"success": False, "error": str(e)}

    @app.get("/api/config")
    async def get_config_file():
        """Get the raw config.yaml content."""
        try:
            if os.path.exists("./config/config.yaml"):
                with open("./config/config.yaml", 'r') as f:
                    content = f.read()
                return {"success": True, "content": content}
            return {"success": False, "error": "config.yaml not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/config")
    async def update_config_file(request: ConfigUpdateRequest):
        """Update config.yaml."""
        try:
            try:
                yaml.safe_load(request.content)
            except yaml.YAMLError as e:
                return {"success": False, "error": f"Invalid YAML: {e}"}

            if os.path.exists("./config/config.yaml"):
                with open("./config/config.yaml", 'w') as f:
                    f.write(request.content)
                logger.info("Configuration file updated via API")
                return {"success": True}
        except Exception as e:
            logger.error(f"Failed to update config: {e}")
            return {"success": False, "error": str(e)}

    # ---- Spectrum & Channel ----

    @app.get("/api/zigbee/spectrum")
    async def get_spectrum():
        """Perform a ZigBee energy scan across all 2.4GHz channels (11-26)."""
        try:
            zigbee_service = get_zigbee_service()
            if not zigbee_service.app:
                return {"success": False, "error": "Zigbee network not started"}

            logger.info("Starting spectrum energy scan...")
            results = await zigbee_service.app.energy_scan(
                channels=range(11, 27), count=3, duration_exp=4
            )
            spectrum = {int(ch): int(energy) for ch, energy in results.items()}
            save_scan(spectrum)
            best = select_best_channel(spectrum)

            current = None
            if zigbee_service.app and hasattr(zigbee_service.app.state, 'network_info'):
                current = getattr(zigbee_service.app.state.network_info, 'channel', None)

            return {
                "success": True, "spectrum": spectrum,
                "best_channel": best, "current_channel": current,
                "channels": list(range(11, 27))
            }
        except NotImplementedError:
            return {"success": False, "error": "Energy scan not supported by this coordinator"}
        except Exception as e:
            logger.error(f"Spectrum scan failed: {e}")
            return {"success": False, "error": str(e)}

    @app.get("/api/zigbee/spectrum/support")
    async def get_spectrum_support():
        """Check if the coordinator hardware supports energy scanning."""
        zigbee_service = get_zigbee_service()
        if not zigbee_service.app:
            return {"supported": False, "reason": "Zigbee network not started"}

        monitor = getattr(zigbee_service, 'spectrum_monitor', None)
        auto_enabled = monitor is not None and monitor._running if monitor else False

        try:
            result = await zigbee_service.app.energy_scan(
                channels=range(11, 12), count=1, duration_exp=2
            )
            return {
                "supported": bool(result),
                "auto_scan_enabled": auto_enabled,
                "auto_scan_interval": monitor.interval if monitor else 0,
                "last_scan_ts": monitor.last_scan_ts if monitor else None
            }
        except NotImplementedError:
            return {"supported": False, "reason": "Coordinator does not support energy_scan"}
        except Exception as e:
            return {"supported": False, "reason": str(e)}

    @app.post("/api/zigbee/channel/auto")
    async def auto_select_channel():
        """Run energy scan, pick the best channel, write to config."""
        try:
            scan_result = await get_spectrum()
            if not scan_result.get("success"):
                return scan_result

            best = scan_result["best_channel"]
            spectrum = scan_result["spectrum"]

            with open("./config/config.yaml", "r") as f:
                cfg = yaml.safe_load(f) or {}
            cfg.setdefault("zigbee", {})["channel"] = best
            with open("./config/config.yaml", "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

            logger.info(f"Auto channel selection: channel {best} written to config")
            return {
                "success": True, "selected_channel": best,
                "spectrum": spectrum,
                "message": f"Channel {best} selected and saved. Restart service to apply."
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/zigbee/spectrum/history")
    async def get_spectrum_history(hours: int = 24):
        """Return raw spectrum scan records for the past N hours."""
        hours = min(hours, 168)
        try:
            records = get_history(hours=hours)
            return {"success": True, "hours": hours, "records": records}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/zigbee/spectrum/averages")
    async def get_spectrum_averages(hours: int = 24):
        """Return average energy per channel for the past N hours."""
        hours = min(hours, 168)
        try:
            averages = get_channel_averages(hours=hours)
            return {"success": True, "hours": hours, "averages": averages}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/zigbee/spectrum/stats")
    async def get_spectrum_stats(hours: int = 24):
        """Return per-channel statistics (min, max, mean, stddev, percentiles)."""
        hours = min(hours, 168)
        try:
            stats = get_channel_stats(hours=hours)
            return {"success": True, "hours": hours, "stats": stats}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/zigbee/spectrum/scan-now")
    async def trigger_background_scan():
        """Trigger an immediate background scan and store results."""
        try:
            zigbee_service = get_zigbee_service()
            monitor = getattr(zigbee_service, 'spectrum_monitor', None)
            if not monitor:
                return {"success": False, "error": "Spectrum monitor not running"}
            results = await monitor.run_scan_now()
            return {"success": True, "spectrum": results}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ---- Credential Regeneration ----

    @app.post("/api/zigbee/credentials/regenerate")
    async def regenerate_credentials(data: dict):
        """Regenerate one or more network credentials and write to config."""
        try:
            regen = data
            with open("./config/config.yaml", "r") as f:
                cfg = yaml.safe_load(f) or {}

            z = cfg.setdefault("zigbee", {})
            regenerated = {}

            if regen.get("pan_id"):
                z["pan_id"] = generate_pan_id()
                regenerated["pan_id"] = z["pan_id"]
            if regen.get("extended_pan_id"):
                z["extended_pan_id"] = generate_extended_pan_id()
                regenerated["extended_pan_id_hex"] = "".join(f"{b:02X}" for b in z["extended_pan_id"])
            if regen.get("network_key"):
                z["network_key"] = generate_network_key()
                regenerated["network_key_hex"] = "".join(f"{b:02X}" for b in z["network_key"])

            with open("./config/config.yaml", "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

            logger.warning(f"Credentials regenerated: {list(regenerated.keys())}")
            return {
                "success": True, "regenerated": regenerated,
                "message": "Credentials saved. Restart service to apply."
            }
        except Exception as e:
            return {"success": False, "error": str(e)}