"""
ZigBee Matter Manager — FastAPI application entry point.

Route modules live under routes/; see docs/structure.md for the layout.
"""
import uvicorn
import warnings
import subprocess
import json
import yaml
import os
import sys
import logging
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
import queue
import asyncio
from contextlib import asynccontextmanager
from typing import Optional
import time
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
import random

# Crash hook — installed before heavy imports so import-time failures are
# captured to data/last_crash.json for the recovery server. launcher.py parses
# stderr as a fallback for failures before this hook exists.
import sys as _sys
import os as _os
import json as _json
import traceback as _tb
import datetime as _dt

_CRASH_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data", "last_crash.json")

# zha-quirks passes deprecated kwargs to zigpy 1.1 and uses enum_factory.
# Warned through two channels — warnings.warn() and a plain logger call.

# Channel 1: DeprecationWarning via warnings.warn — silenced for completeness.
warnings.filterwarnings(
    "ignore",
    message=r"Command .* has an incorrect direction.*",
)
warnings.filterwarnings(
    "ignore",
    message=r"enum_factory is internal to zigpy and deprecated.*",
)

# Channel 2: direct logger calls (zigpy.zcl LOGGER.warning + zigpy.types LOGGER.error).
# This is the one that actually shows up in your boot log.
class _ZigpyDeprecationFilter(logging.Filter):
    """Drop the upstream-quirks deprecation chatter that we cannot fix."""
    _NEEDLES = (
        "has an incorrect direction, please remove the `direction` kwarg",
        "enum_factory is internal to zigpy and deprecated",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(needle in msg for needle in self._NEEDLES)

_zigpy_dep_filter = _ZigpyDeprecationFilter()
# Attach to the specific loggers that emit these messages.
logging.getLogger("zigpy.zcl").addFilter(_zigpy_dep_filter)
logging.getLogger("zigpy.types").addFilter(_zigpy_dep_filter)
# Also attach to the root logger so the messages get dropped before reaching
# any handler that uvicorn or your own setup configures later.
logging.getLogger().addFilter(_zigpy_dep_filter)

def _zmm_record_crash(exc_type, exc_value, tb):
    try:
        _os.makedirs(_os.path.dirname(_CRASH_FILE), exist_ok=True)
        frames = _tb.extract_tb(tb) if tb else []
        suspect_file = None
        suspect_line = None
        for fr in reversed(frames):
            fn = fr.filename or ""
            if ("/app/" in fn or not fn.startswith("/")) and "site-packages" not in fn:
                suspect_file = fn
                suspect_line = fr.lineno
                break
        if suspect_file is None and frames:
            suspect_file, suspect_line = frames[-1].filename, frames[-1].lineno
        suspect_rel = suspect_file
        if suspect_rel and suspect_rel.startswith("/app/"):
            suspect_rel = suspect_rel[5:]
        with open(_CRASH_FILE, "w") as f:
            _json.dump({
                "timestamp": _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
                "exc_type": exc_type.__name__ if exc_type else "Unknown",
                "exc_value": str(exc_value) if exc_value else "",
                "suspect_file": suspect_file,
                "suspect_file_rel": suspect_rel,
                "suspect_line": suspect_line,
                "traceback": "".join(_tb.format_exception(exc_type, exc_value, tb))[-12000:],
                "exit_code": 1,
                "source": "main_excepthook",
            }, f, indent=2)
    except Exception:
        pass


def _zmm_excepthook(exc_type, exc_value, tb):
    _zmm_record_crash(exc_type, exc_value, tb)
    _sys.__excepthook__(exc_type, exc_value, tb)


_sys.excepthook = _zmm_excepthook


# Import services
try:
    from core import ZigbeeService
    from mqtt import MQTTService
    from modules.boot_guard_hooks import clear_boot_failure_counter
    from modules.zigbee_debug import get_debugger
    from modules.json_helpers import prepare_for_json, safe_json_dumps
    from modules.mqtt_explorer import MQTTExplorer
    from modules.zones_api import register_zone_routes
    from modules.automation_api import register_automation_routes
    from modules.network_init import ensure_network_credentials
    from modules.spectrum_monitor import SpectrumMonitor
    from modules.ai_assistant import AIAssistant
    from modules.ai_automations import AIAutomations
    from modules.ai_api import register_ai_routes
    from modules.safe_deploy import register_deploy_routes, check_deploy_on_startup
    from modules.system_monitor import SystemMonitor
    from modules.telemetry_collector import TelemetryCollector
    from modules.telemetry_api import register_telemetry_routes
    from modules.dongle_jedi_api import register_setup_routes
    from modules.matter_definitions import get_definition_store
    from modules.rotary_bindings import get_rotary_binding_manager
    from modules.weather import WeatherService
    from modules.octopus import OctopusEnergyService
    from modules.media import MediaService
    from modules.media.therapy_tts import create_therapy_tts
    from modules.heating_advisor import HeatingAdvisor
    from modules.heating_controller import HeatingController
    from modules.heating_anomaly_watcher import HeatingAnomalyWatcher
    from modules.auth import AuthManager, set_auth_manager
    from modules.auth_middleware import AuthMiddleware
    from modules.presence_users import PresenceUserManager, set_presence_manager, get_presence_manager
    from modules.journeys import JourneyManager, set_journey_manager
    from modules.auth import AuthManager, set_auth_manager
    from modules.auth_middleware import AuthMiddleware
    from modules.auth_secure import SecureAuthManager, set_secure_auth_manager
    from modules.auth_network import NetworkResolver, set_network_resolver

    # Import route registrations
    from routes import (
        register_backup_routes,
        register_config_routes,
        register_auth_routes,
        register_upgrade_routes,
        register_device_routes,
        register_profile_routes,
        register_network_routes,
        register_system_routes,
        register_matter_routes,
        register_rotary_binding_routes,
        register_group_routes,
        register_editor_routes,
        register_ota_routes,
        register_otbr_routes,
        register_matter_attribute_routes,
        register_matter_definition_routes,
        register_test_recovery_routes,
        register_websocket_routes,
        register_weather_routes,
        register_octopus_routes,
        register_heating_routes,
        register_heating_controller_routes,
        register_presence_routes,
        register_remote_access_routes,
        register_sun_routes,
        register_floor_plan_routes,
        register_chamber_routes,
        register_frame_routes,
        register_ac_routes,
        register_security_routes,
        register_media_routes,
        register_cast_sync_routes,
        register_tts_routes,
        register_api_docs_routes,
        register_wiki_routes,
        register_alert_routes,
        register_signal_routes,
        register_adblock_routes,
        manager, broadcast_event,
    )

except Exception:
    _zmm_record_crash(*_sys.exc_info())
    raise

port = int(os.environ.get("ZMM_PORT", 8000))


os.makedirs("logs", exist_ok=True)

log_queue = queue.Queue(-1)

file_handler = RotatingFileHandler('logs/zigbee.log', maxBytes=1024*1024, backupCount=3)
console_handler = logging.StreamHandler()

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

log_listener = QueueListener(log_queue, file_handler, console_handler)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = []

queue_handler = QueueHandler(log_queue)
root_logger.addHandler(queue_handler)

logging.getLogger('handlers').setLevel(logging.INFO)
logging.getLogger('handlers.base').setLevel(logging.INFO)
logging.getLogger('core').setLevel(logging.INFO)
logging.getLogger('device').setLevel(logging.INFO)

logger = logging.getLogger('main')


def load_config():
    """Load configuration from config.yaml."""
    if not os.path.exists("./config/config.yaml"):
        return {}
    with open("./config/config.yaml", 'r') as f:
        return yaml.safe_load(f) or {}


CONFIG = load_config()


def get_conf(section, key, default=None):
    """Get configuration value."""
    return CONFIG.get(section, {}).get(key, default)


mqtt_service = MQTTService(
    broker_host=get_conf('mqtt', 'broker_host', 'localhost'),
    port=get_conf('mqtt', 'broker_port', 1883),
    username=get_conf('mqtt', 'username'),
    password=get_conf('mqtt', 'password'),
    base_topic=get_conf('mqtt', 'base_topic', 'zigbee_ha'),
    qos=get_conf('mqtt', 'qos', 0),
    log_callback=None,
    ha_discovery=get_conf('homeassistant', 'enabled', True)
)

mqtt_enabled = get_conf('mqtt', 'enabled', True)  # Default True for backward compat


async def _zigbee_event_callback(evt: str, data: dict):
    await broadcast_event(evt, data)
    if evt == "device_joined":
        ieee = data.get("ieee")
        if ieee:
            heating_controller.on_device_rejoin(ieee)

zigbee_service = ZigbeeService(
    port=get_conf('zigbee', 'port', '/dev/ttyACM0'),
    mqtt_client=mqtt_service,
    config=CONFIG.get('zigbee', {}),
    event_callback=_zigbee_event_callback
)

# Wire the Signal Inspector's live stream to the service's sync emitter so
# active inspections push `signal_inspector_update` events over the WebSocket.
try:
    from modules.signal_inspector import get_signal_inspector
    get_signal_inspector().set_emitter(zigbee_service._emit_sync)
except Exception as _e:
    logging.getLogger("main").warning(f"Signal inspector emitter wiring failed: {_e}")

weather_service = WeatherService(
    config=CONFIG.get("weather", {}),
    mqtt_service=mqtt_service,
)

# Share the weather service's location so sun conditions track exactly the
# coordinates driving external temp / cloud cover.
from modules.sun_times import set_location_provider
set_location_provider(lambda: (weather_service.latitude, weather_service.longitude))

media_service = MediaService(
    config=CONFIG.get("media", {}),
)

# Built before the heating advisor so live tariff rates can be injected.
# Config is read at construction: changes need a service restart.
octopus_service = OctopusEnergyService(config=CONFIG.get("octopus", {}))

# Default engine is in-process Kokoro-82M; set media.therapy.engine to
# "wyoming" for an external wyoming-piper server.
therapy_tts = create_therapy_tts(CONFIG.get("media", {}).get("therapy", {}))
# The device-audio listener serves /api/therapy/stream to speakers; hand it
# the TTS service (built after MediaService) so guided speech works there too.
media_service.device_http.get_tts = lambda: therapy_tts

# Let automation steps play radio/Tidal and control players (engine is built
# before the media service, so wire the getter in now).
zigbee_service.automation.set_media_service_getter(lambda: media_service)


heating_advisor = HeatingAdvisor(
    config=CONFIG.get("heating", {}),
    weather_service=weather_service,
    device_getter=lambda: (zigbee_service.devices if zigbee_service else {}),
    tariff_provider=lambda fuel: octopus_service.heating_tariff(fuel),
)

heating_controller = HeatingController(
    config=CONFIG.get("heating", {}),
    device_getter=lambda: (zigbee_service.devices if zigbee_service else {}),
    command_sender=lambda ieee, command, value=None, endpoint_id=None:
    zigbee_service.send_command(ieee, command, value, endpoint_id=endpoint_id),
    comfort_defaults=CONFIG.get("heating", {}).get("comfort", {}),
    weather_service=weather_service,
    anomaly_getter=lambda: heating_anomaly_watcher.get_snapshot(),
    telemetry_query=lambda ieee, attr, hours:
    __import__("modules.telemetry_db", fromlist=["query_device_state_history"])
    .query_device_state_history(ieee, attr, hours),
    )


heating_anomaly_watcher = HeatingAnomalyWatcher(
    config_getter=lambda: CONFIG,  # or whatever gives current config dict
    advisor_getter=lambda: heating_advisor,
    telemetry_query=lambda ieee, attr, hours:
    __import__("modules.telemetry_db", fromlist=["query_device_state_history"])
    .query_device_state_history(ieee, attr, hours),
    )

# Give the advisor a reference to the controller so it can surface
# controller intent (calling-for-heat) alongside device reality.
try:
    heating_advisor.controller = heating_controller
except Exception:
    pass


matter_server = None
matter_bridge = None

matter_config = CONFIG.get('matter', {})
if matter_config.get('enabled', False):
    from modules.matter_server import MatterServerManager

    storage_path = matter_config.get('storage_path', './data/matter')
    # Environment variable takes priority (set by build.sh for host networking),
    # then config.yaml, then default 5580
    matter_port = int(os.environ.get('ZMM_MATTER_PORT', 0)) or matter_config.get('port', 5580)

    matter_server = MatterServerManager(
        storage_path=storage_path,
        port=matter_port,
        bluetooth_adapter=matter_config.get('bluetooth_adapter', None),
    )

    from modules.matter_bridge import MatterBridge
    server_url = f"ws://localhost:{matter_port}/ws"
    matter_bridge = MatterBridge(
        server_url=server_url,
        mqtt_service=mqtt_service,
        event_callback=broadcast_event,
    )
    logger.info(f"Matter integration enabled (embedded server + bridge)")


from modules.remote_access import (
    RemoteAccessManager, set_remote_access_manager,
)

_web_cfg = CONFIG.get('web', {}) or {}
remote_access_manager = RemoteAccessManager(
    origin_port=int(_web_cfg.get('port') or 8000),
    origin_https=bool((_web_cfg.get('ssl') or {}).get('enabled', False)),
)
remote_access_manager.load()
set_remote_access_manager(remote_access_manager)


def get_zigbee_service():
    return zigbee_service

def get_mqtt_service():
    return mqtt_service

def get_matter_server():
    return matter_server

def get_matter_bridge():
    return matter_bridge

def get_manager():
    return manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    clear_boot_failure_counter()
    # Startup
    log_listener.start()
    logger.info("Starting Zigbee Gateway (Threaded Logging Enabled)...")

    # Dumps the blocking stack on a stall and self-restarts on a hard wedge, so
    # the launcher handles a blocked loop rather than the manager watchdog.
    # Started first so a stall during bring-up is caught.
    from modules.loop_monitor import LoopMonitor
    loop_monitor = LoopMonitor()
    loop_monitor.start()
    app.state.loop_monitor = loop_monitor

    # Media first: a restart stops any Cast stream this process is serving, and
    # boot order alone decides for how long — measured at 67 s before this moved,
    # far past the point a Cast device stops retrying. Depends on nothing below.
    try:
        media_service.start()
        logger.info("Media service initialised (early — audio listeners first)")
    except Exception as e:
        logger.error(f"Media service early start failed: {e}")

    # Open/migrate the telemetry DB in a worker before any service touches it:
    # the first _get_db() holds _db_lock for the whole open (seconds), and a
    # loop-thread write landing in that window stalls the event loop. Self-heal
    # runs before warm() — it swaps database files, safe only while nothing has
    # the file open.
    try:
        from modules import telemetry_rebuild
        await asyncio.to_thread(telemetry_rebuild.auto_rebuild_if_needed)
    except Exception as e:
        logger.warning(f"Telemetry auto-rebuild check failed: {e}")

    telemetry_db = None
    warm_error = None
    try:
        from modules import telemetry_db
        await asyncio.to_thread(telemetry_db.warm)
    except Exception as e:
        warm_error = e
        logger.warning(f"Telemetry DB warm-up failed: {e}")

    # Same treatment for the zigbee cache DB: zigbee_service.start() touches it
    # from the loop thread, so the open must already be paid for by then.
    try:
        from modules import zigbee_cache
        await asyncio.to_thread(zigbee_cache.warm)
    except Exception as e:
        logger.warning(f"Zigbee cache DB warm-up failed: {e}")

    # Application alert center: capture ERROR-level logs as user-visible
    # alerts and push them over the WebSocket hub
    try:
        from modules.app_alerts import get_alert_center, install_log_capture
        get_alert_center().set_emitter(broadcast_event)
        install_log_capture()
    except Exception as e:
        logger.warning(f"Alert center init failed: {e}")

    # Catch-all for a fatally-damaged telemetry DB. Without it a fatal raised
    # off the _write_exec path is never latched, no rebuild sentinel is written,
    # and the next boot cannot self-repair.
    try:
        if telemetry_db is not None:
            telemetry_db.install_fatal_watch()
    except Exception as e:
        logger.debug(f"Could not install telemetry fatal watch: {e}")

    # Raised only now the alert center can push it. A failed warm-up leaves the
    # connection unopened, so the next caller pays the failing open under _db_lock.
    if warm_error is not None:
        try:
            from modules.app_alerts import raise_alert
            raise_alert(
                severity="warning",
                source="telemetry_db",
                title="Telemetry database warm-up failed",
                message=(
                    f"{warm_error}\n\n"
                    "History and trend data may be unavailable, and the first "
                    "query will be slow while the database opens. Telemetry "
                    "writes are buffered off the event loop, so the app stays "
                    "responsive either way."
                ),
                dedupe_key="telemetry_db:warm_failed",
            )
        except Exception as e:
            logger.debug(f"Could not raise telemetry warm-up alert: {e}")

    # Reconcile the DB against the active write backend on a dedicated thread
    # (handles a WAL left behind by the other backend). Returns immediately.
    if telemetry_db is not None:
        try:
            telemetry_db.start_reconciler()
        except Exception as e:
            logger.debug(f"Could not start telemetry reconciler: {e}")

        # On a worker thread, and here — before system_monitor exists to write
        # one. Lazily on first write this ran on the event loop and crash-looped.
        try:
            await asyncio.to_thread(telemetry_db.migrate_system_metrics)
        except Exception as e:
            logger.warning(f"system_metrics migration failed: {e}")

    # Test-deploy recovery. Before the slow bring-up: the confirm window for a
    # pending restart-type batch starts here, when the UI is about to serve.
    from modules.test_recovery import get_test_recovery_manager
    trm = get_test_recovery_manager(broadcast_event)
    startup_result = trm.check_pending_on_startup()
    if startup_result:
        if startup_result.get("rolled_back"):
            logger.warning(f"Auto-rolled back test deployment: {startup_result.get('files')}")
        elif startup_result.get("pending"):
            logger.info(f"Pending test: {startup_result.get('files')} — "
                        f"{startup_result.get('remaining')}s to confirm")

    # Wire debugger to WebSocket
    async def debug_callback(packet_data):
        await manager.broadcast({"type": "debug_packet", "payload": packet_data})

    debugger = get_debugger()
    if debugger:
        debugger.add_callback(debug_callback)
        logger.info("Registered debug callback for live streaming")

    await manager.broadcast({
        "type": "log",
        "payload": {"level": "INFO", "message": "System Starting...", "timestamp": None}
    })

    # Setup wizard routes (must register before Zigbee start check)
    register_setup_routes(app, ws_manager=manager)

    # upgrade registration
    register_upgrade_routes(app)
    logger.info("Upgrade routes registered")

    # Reschedule persisted app-side AC timers (registered by ac_routes;
    # needs the running loop, which is why it's here and not on_event)
    ac_timers_start = getattr(app.state, "ac_timers_start", None)
    if ac_timers_start:
        try:
            ac_timers_start()
        except Exception as e:
            logger.warning(f"AC timer restore failed: {e}")

    # Nuki bridge poller (registered by security_routes; needs the running
    # loop) — keeps lock state fresh so automations trigger on lock/unlock
    nuki_poll_start = getattr(app.state, "nuki_poll_start", None)
    if nuki_poll_start:
        try:
            nuki_poll_start()
        except Exception as e:
            logger.warning(f"Nuki poller start failed: {e}")

    # Check if setup is needed BEFORE starting MQTT or Zigbee
    from modules.dongle_jedi import DongleJedi
    setup_status = DongleJedi.needs_setup()

    # Deferred bring-up: the Zigbee radio takes 40-70 s on MultiPAN and Matter
    # 10-30 s. Run in the background so uvicorn serves the UI immediately;
    # progress streams over the websocket as each service registers.
    async def _background_bringup():
        if setup_status["needs_setup"]:
            logger.warning(f"Setup needed: {setup_status['reason']}")
            logger.info("Web UI is up — setup wizard will guide the user")
            await manager.broadcast({
                "type": "log",
                "payload": {
                    "level": "WARN",
                    "message": f"Setup needed ({setup_status['reason']}). Open the web UI.",
                    "timestamp": None,
                }
            })
        else:
            mqtt_enabled = get_conf('mqtt', 'enabled', True)

            if mqtt_enabled:
                try:
                    await mqtt_service.start()
                    logger.info("MQTT connected")
                except Exception as e:
                    logger.warning(f"MQTT connection failed: {e}")

                mqtt_service.mqtt_explorer = MQTTExplorer(mqtt_service, max_messages=1000)
                async def mqtt_explorer_callback(message_record):
                    await manager.broadcast({"type": "mqtt_message", "payload": message_record})
                mqtt_service.mqtt_explorer.add_callback(mqtt_explorer_callback)
                logger.info("MQTT Explorer initialized")
            else:
                logger.info("MQTT disabled (standalone mode)")

            # Use the config returned by credential auto-fill; module-level
            # CONFIG predates it and would feed the radio stale placeholders.
            updated_cfg = ensure_network_credentials("./config/config.yaml")
            if updated_cfg:
                CONFIG['zigbee'] = updated_cfg.get('zigbee', {})
            zigbee_service._config = CONFIG.get('zigbee', {})
            network_key = get_conf('zigbee', 'network_key', None)
            await zigbee_service.start(network_key=network_key)
            logger.info("Zigbee network started")

            heating_controller._resilience_manager = getattr(zigbee_service.app, "_resilience_manager", None)

            # Wire group callback
            if mqtt_enabled:
                mqtt_service.group_command_callback = zigbee_service.group_manager.handle_mqtt_group_command
                logger.info("Wired GroupManager callback to MQTT Service")

        # Start Matter
        if matter_server:
            try:
                started = await matter_server.start()
                if started:
                    logger.info("Embedded Matter server started")
                    # CHIP SDK needs 10-30s before its WS API listens; wait so
                    # the bridge's first connect doesn't fail and raise an alert.
                    await matter_server.wait_ready(timeout=90)
            except Exception as e:
                logger.error(f"Failed to start Matter server: {e}")

        if matter_bridge:
            try:
                await matter_bridge.start()
                logger.info("Matter bridge started")
            except Exception as e:
                logger.error(f"Failed to start Matter bridge: {e}")

        # Start remote access tunnel if the user enabled it
        if remote_access_manager.settings.enabled:
            try:
                started = await remote_access_manager.start()
                if started:
                    logger.info("Remote access tunnel started")
            except Exception as e:
                logger.error(f"Failed to start remote access tunnel: {e}")

        # Wire Matter state changes into the automation engine. Deliberately
        # outside the remote-access block — indented inside it, this skipped the
        # wiring when the tunnel was off and crashed when Matter was disabled.
        if matter_bridge:
            matter_bridge._automation_evaluator = (
                lambda ieee, data: zigbee_service.automation.evaluate(ieee, data)
            )
            logger.info("Wired Matter bridge → automation evaluator")


        if matter_bridge and hasattr(zigbee_service, 'resilience') and zigbee_service.resilience:
            matter_bridge._app_resilience = zigbee_service.resilience
            logger.info("Wired resilience manager → Matter bridge")

        # Spectrum monitor — wait for radio to be ready, detect support
        spectrum_interval = get_conf('zigbee', 'spectrum_scan_interval', 3600)
        if spectrum_interval > 0:
            zigbee_service.spectrum_monitor = SpectrumMonitor(
                app_getter=lambda: zigbee_service.app,
                interval=spectrum_interval
            )

            async def _start_spectrum_monitor(svc):
                """Wait for radio, probe energy_scan support, then start."""
                # MultiPAN startup takes longer — CPC stack adds 40-70s
                # before bellows can connect. Extend patience accordingly.
                is_multipan = getattr(svc, 'multipan', None) is not None
                max_wait = 300 if is_multipan else 150  # 5min vs 2.5min
                poll_interval = 5
                max_polls = max_wait // poll_interval

                if is_multipan:
                    logger.info(
                        f"Spectrum monitor: MultiPAN detected, "
                        f"extending radio wait to {max_wait}s"
                    )

                for i in range(max_polls):
                    if svc.app:
                        try:
                            result = await svc.app.energy_scan(
                                channels=range(11, 12), count=1, duration_exp=2
                            )
                            if result:
                                svc.spectrum_monitor.start()
                                logger.info(
                                    f"Spectrum monitor started "
                                    f"(interval={spectrum_interval}s, "
                                    f"waited {i * poll_interval}s for radio)"
                                )
                            else:
                                logger.warning(
                                    "Spectrum monitor: energy_scan returned empty — disabled"
                                )
                        except NotImplementedError:
                            logger.warning(
                                "Spectrum monitor: energy_scan not supported "
                                "by this coordinator — disabled"
                            )
                        except Exception as e:
                            logger.warning(
                                f"Spectrum monitor: energy_scan probe failed "
                                f"({e}) — disabled"
                            )
                        return
                    await asyncio.sleep(poll_interval)

                logger.warning(
                    f"Spectrum monitor: radio never ready after {max_wait}s — disabled"
                )

            asyncio.create_task(_start_spectrum_monitor(zigbee_service))

        # Groups - callback is already wired in ZigbeeService.__init__
        # Just log that it's ready
        if hasattr(zigbee_service, 'group_manager'):
            logger.info("Group manager initialized")

        system_monitor = SystemMonitor(
            interval=30,
            event_callback=broadcast_event,
        )
        system_monitor.start()
        app.state.system_monitor = system_monitor
        logger.info("System monitor started")

        telemetry_collector = TelemetryCollector(
            device_registry_getter=lambda: zigbee_service.devices,
            retention_days=30,
        )
        telemetry_collector.start()
        app.state.telemetry_collector = telemetry_collector
        logger.info("Telemetry collector started")

        register_telemetry_routes(app, lambda: system_monitor)
        zigbee_service.telemetry_collector = telemetry_collector

        # weather
        weather_service.start()
        logger.info("Weather service initialised")

        # octopus energy (tariffs + smart-meter consumption)
        octopus_service.start()
        logger.info("Octopus Energy service initialised")

        # Already started at the top of the lifespan so the audio listeners bind
        # before the slow parts of bring-up. Idempotent no-op safety net for the
        # case where that early call raised.
        media_service.start()

        # heating
        heating_advisor.start()
        logger.info("Heating Advisor initialised")

        heating_controller.start()
        logger.info("Heating Controller initialised")

        heating_anomaly_watcher.start()
        logger.info("Heating Anomaly Detection initialised")

        # Merge Matter devices into automation engine's device registry
        if matter_bridge:
            original_getter = zigbee_service.automation._get_devices
            original_names = zigbee_service.automation._get_names
            def merged_devices():
                devs = dict(original_getter())
                devs.update(matter_bridge.devices)
                return devs
            def merged_names():
                names = dict(original_names())
                for ieee, dev in matter_bridge.devices.items():
                    names[ieee] = dev.friendly_name
                return names
            zigbee_service.automation._get_devices = merged_devices
            zigbee_service.automation._get_names = merged_names
            logger.info("Wired Matter devices into automation engine")

        # Rotary binding manager
        if matter_bridge:
            from modules.matter_definitions import get_definition_store
            rbm = get_rotary_binding_manager()
            rbm.set_dispatchers(
                zigbee_send=zigbee_service.send_command,
                matter_send=matter_bridge.send_command,
            )
            rbm.load_from_definitions(get_definition_store())
            logger.info(f"Rotary binding manager: {len(rbm._all_bindings)} binding(s)")

        presence_manager = PresenceUserManager(
            mqtt_handler=mqtt_service,
            event_emitter=broadcast_event,                    # same one used by zones
            automation_evaluator=zigbee_service.automation.evaluate,
        )
        await presence_manager.start()
        set_presence_manager(presence_manager)
        app.state.presence_manager = presence_manager

        # Wire presence virtual devices into the automation engine, the same
        # way Matter devices are wired in above.
        _orig_dev_getter = zigbee_service.automation._get_devices
        _orig_name_getter = zigbee_service.automation._get_names

        def _devs_with_presence():
            merged = dict(_orig_dev_getter())
            merged.update(presence_manager.automation_devices())
            return merged

        def _names_with_presence():
            names = dict(_orig_name_getter())
            for ieee, dev in presence_manager.automation_devices().items():
                names[ieee] = dev.friendly_name
            return names

        zigbee_service.automation._get_devices = _devs_with_presence
        zigbee_service.automation._get_names = _names_with_presence
        logger.info("Wired presence users into automation engine")

        # Journeys: its own DuckDB file and worker thread — DuckDB is
        # single-writer per file, so journeys never share a database.
        journey_manager = JourneyManager()
        await journey_manager.start()
        set_journey_manager(journey_manager)
        app.state.journey_manager = journey_manager

        # Place search: reference data, but its own DuckDB file and worker
        # thread for the same single-writer reason as journeys.
        from modules.geocode import Geocoder, set_geocoder
        geocoder = Geocoder()
        await geocoder.start()
        geocoder.online_fallback = bool(
            CONFIG.get("geocode", {}).get("online_fallback", False))
        set_geocoder(geocoder)
        app.state.geocoder = geocoder


        # Initialise AI Assistant
        ai_config = CONFIG.get("ai", {})
        ai_assistant = AIAssistant(ai_config)
        ai_automations = AIAutomations(ai_assistant, zigbee_service.automation)
        from modules.ai_chat import AIChat
        ai_chat = AIChat(ai_assistant, ai_automations)
        logger.info(f"AI Assistant initialised: {ai_assistant.provider}/{ai_assistant.model} "
                    f"configured={ai_assistant.is_configured()}")

        # update_block rather than safe_load + dump: the latter round-trips the
        # whole file through an emitter with no concept of comments, so saving one
        # API key would delete every comment in config.yaml.
        def _save_ai_config(ai_cfg):
            from modules.config_yaml import update_block
            try:
                update_block("./config/config.yaml", "ai", ai_cfg,
                             block_comment="AI assistant provider settings.")
                logger.info("AI config saved to config.yaml")
            except Exception as e:
                logger.error(f"Failed to save AI config: {e}")

        register_ai_routes(
            app,
            ai_assistant_getter=lambda: ai_assistant,
            ai_automations_getter=lambda: ai_automations,
            config_saver=_save_ai_config,
            ai_chat_getter=lambda: ai_chat,
        )


        # Safe Deploy
        register_deploy_routes(app, service_name="zigbee_matter_manager")
        logger.info("Safe deploy routes registered")

        # Check if we're recovering from a deploy
        asyncio.create_task(check_deploy_on_startup())

        # Upgrade manager background loops
        try:
            from modules.upgrade_manager import periodic_check_loop, status_watcher_loop

            async def _broadcast_upgrade(payload):
                try:
                    await manager.broadcast(payload)
                except Exception as e:
                    logger.debug(f"Upgrade broadcast failed: {e}")

            app.state.upgrade_check_task = asyncio.create_task(
                periodic_check_loop(interval_hours=6, broadcast_fn=_broadcast_upgrade)
            )
            app.state.upgrade_status_task = asyncio.create_task(
                status_watcher_loop(broadcast_fn=_broadcast_upgrade, poll_seconds=2.0)
            )
            logger.info("Upgrade manager background loops started")
        except Exception as e:
            logger.warning(f"Failed to start upgrade manager loops: {e}")

        # Pushes a 2 s snapshot of rates / top talkers / anomalies to all
        # clients. Independent of debug capture; packet_flow counters are always on.
        try:
            from modules.packet_flow import get_flow_analyzer

            async def _packet_flow_loop(interval: float = 2.0):
                analyzer = get_flow_analyzer()
                while True:
                    try:
                        snap = analyzer.get_snapshot(top_n=10, history_seconds=60)
                        await manager.broadcast({"type": "packet_flow", "payload": snap})
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.debug(f"packet_flow broadcast failed: {e}")
                    await asyncio.sleep(interval)

            app.state.flow_broadcast_task = asyncio.create_task(_packet_flow_loop(2.0))
            logger.info("Packet flow broadcaster started (2s interval)")
        except Exception as e:
            logger.warning(f"Failed to start packet flow broadcaster: {e}")

        app.state.bringup_status = "ready"
        logger.info("✅ Background bring-up complete — all services started")
        await manager.broadcast({
            "type": "log",
            "payload": {"level": "INFO", "message": "All services started",
                        "timestamp": None}
        })

    def _bringup_done(task: asyncio.Task):
        # Retrieve the exception so a failed bring-up is a logged error, not an
        # unretrieved task exception. "failed" flips /api/system/health to 503,
        # which the ZMM Manager watchdog keys off to restart the container.
        if not task.cancelled() and task.exception() is not None:
            app.state.bringup_status = "failed"
            app.state.bringup_error = f"{type(task.exception()).__name__}: {task.exception()}"
            logger.error(f"Background bring-up failed: {task.exception()!r}")

    app.state.bringup_status = "starting"
    app.state.bringup_error = None
    app.state.bringup_task = asyncio.create_task(_background_bringup())
    app.state.bringup_task.add_done_callback(_bringup_done)
    logger.info("Web UI starting — service bring-up continues in background")


    yield  # Application runs here

    # Shutdown
    logger.info("Shutting down Zigbee Matter Manager...")

    # First stop the loop monitor — a shutting-down loop must not be
    # mistaken for a stall (its self-restart would fight the shutdown).
    loop_monitor.stop()

    # 0. stop the background bring-up if it's still in flight
    bringup_task = getattr(app.state, "bringup_task", None)
    if bringup_task and not bringup_task.done():
        bringup_task.cancel()
        try:
            await bringup_task
        except (asyncio.CancelledError, Exception):
            pass

    # 1. monitors and telemetry first (may be absent if bring-up was
    #    interrupted, hence the guards)
    system_monitor = getattr(app.state, "system_monitor", None)
    if system_monitor:
        system_monitor.stop()
    telemetry_collector = getattr(app.state, "telemetry_collector", None)
    if telemetry_collector:
        telemetry_collector.stop()
    weather_service.stop()
    octopus_service.stop()
    media_service.stop()
    heating_advisor.stop()
    heating_controller.stop()
    heating_anomaly_watcher.stop()
    from modules.telemetry_db import close as close_telemetry_db
    close_telemetry_db()
    presence_manager = getattr(app.state, "presence_manager", None)
    if presence_manager:
        await presence_manager.stop()
    journey_manager = getattr(app.state, "journey_manager", None)
    if journey_manager:
        await journey_manager.stop()
    geocoder = getattr(app.state, "geocoder", None)
    if geocoder:
        await geocoder.stop()
    # Lazy singleton — only exists if someone searched for fuel this run.
    from modules import fuel_history as _fuel_history
    if _fuel_history._manager is not None:
        await _fuel_history._manager.stop()
    from modules.messages_store import get_message_store as _gms
    if _gms() is not None:
        await _gms().stop()

    # 2. services
    if zigbee_service.multipan and zigbee_service.multipan.is_running:
        await zigbee_service.multipan.stop()
    await zigbee_service.stop()
    await mqtt_service.stop()
    if hasattr(zigbee_service, 'spectrum_monitor'):
        zigbee_service.spectrum_monitor.stop()
    if matter_bridge:
        await matter_bridge.stop()
    if matter_server:
        await matter_server.stop()
    await remote_access_manager.stop()
    log_listener.stop()
    # Stop upgrade manager loops
    for task_name in ("upgrade_check_task", "upgrade_status_task", "flow_broadcast_task"):
        task = getattr(app.state, task_name, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(
    title="Zigbee Matter Manager",
    description="Zigbee device management",
    version="1.0.0",
    lifespan=lifespan
)

auth_manager = AuthManager()
auth_manager.load()
set_auth_manager(auth_manager)

secure_auth = SecureAuthManager(auth_manager)
set_secure_auth_manager(secure_auth)

# Network resolver — read trusted-proxy/LAN config from config.yaml
sec_cfg = (CONFIG.get("security") or {}).get("network", {}) or {}
network_resolver = NetworkResolver(
    trusted_proxies=sec_cfg.get("trusted_proxies"),
    lan_ranges=sec_cfg.get("lan_ranges"),
    cloudflare_tunnel_enabled=bool(
        sec_cfg.get("cloudflare_tunnel_enabled", False)
    ),
    cloudflare_ranges=sec_cfg.get("cloudflare_ranges"),
)
set_network_resolver(network_resolver)
logger.info(f"Network policy: {network_resolver.describe()}")

auth_mw = AuthMiddleware(app, auth_manager, enforce=True)
app.add_middleware(BaseHTTPMiddleware, dispatch=auth_mw.dispatch)

register_auth_routes(
    app,
    auth_manager_getter=lambda: auth_manager,
    secure_manager_getter=lambda: secure_auth,
    network_resolver_getter=lambda: network_resolver,
    secret_getter=auth_mw._secret,
)
logger.info("Auth subsystem initialised (MFA + lockout + LAN-aware)")

app.mount("/static", StaticFiles(directory="static"), name="static")


# Session cookie recording an explicit "I want the manager" choice, so the
# per-user `landing` preference doesn't bounce the user straight back to /frames.
VIEW_COOKIE = "zmm_view"


@app.get("/")
async def read_index(request: Request):
    """
    Serve the main UI, honouring the user's `landing` preference.

    A user with landing="frames" (a "mobile user") gets redirected to /frames.
    This is a convenience, never a permission — the manager stays fully
    reachable via the switcher, which arrives here as ?view=manager and is
    remembered for the session.
    """
    if request.query_params.get("view") == "manager":
        resp = FileResponse('static/index.html')
        resp.set_cookie(VIEW_COOKIE, "manager", httponly=True, samesite="lax", path="/")
        return resp

    principal = getattr(request.state, "principal", None)
    landing = getattr(getattr(principal, "user", None), "landing", "manager")
    if principal and landing == "frames" and request.cookies.get(VIEW_COOKIE) != "manager":
        return RedirectResponse("/frames", status_code=302)

    return FileResponse('static/index.html')


@app.get("/frames")
async def read_frames():
    """
    Serve the Frames UI — the mobile-first front end.

    A separate, deliberately lightweight page: it must not pull in the admin
    dashboard's module graph (websocket.js alone imports 13 modules, and
    actions.js drags the whole device-modal set). See static/js/frames-page.js.

    This is the PWA's start_url, so a phone-installed app opens here; the header
    switcher goes to '/' for the manager.

    Coming here is an explicit "I want Frames", so it clears the manager
    override — otherwise a mobile user who visited the manager once would never
    be redirected here again.
    """
    resp = FileResponse('static/frames.html')
    resp.delete_cookie(VIEW_COOKIE, path="/")
    return resp

@app.get("/sw.js")
async def service_worker():
    """Serve service worker from root scope for PWA support."""
    return FileResponse(
        'static/sw.js',
        media_type='application/javascript',
        # no-cache: browsers and Cloudflare's edge must revalidate sw.js, or a
        # bumped CACHE_NAME never reaches clients and the PWA stays on old assets.
        headers={'Service-Worker-Allowed': '/', 'Cache-Control': 'no-cache'}
    )

@app.get("/manifest.json")
async def manifest():
    """Serve PWA manifest from root scope."""
    return FileResponse(
        'static/manifest.json',
        media_type='application/manifest+json'
    )


register_config_routes(app, get_zigbee_service)
register_device_routes(app, get_zigbee_service, get_matter_bridge)
register_signal_routes(app, get_zigbee_service)
register_profile_routes(app)
register_editor_routes(app, get_zigbee_service)
register_network_routes(app, get_zigbee_service)
register_system_routes(app, get_zigbee_service, get_mqtt_service, get_manager)
register_matter_routes(app, get_zigbee_service, get_matter_server, get_matter_bridge)
register_group_routes(app, get_zigbee_service, get_manager)
register_otbr_routes(app, get_zigbee_service)
register_matter_attribute_routes(app, get_matter_bridge)
register_matter_definition_routes(app, get_matter_bridge)
register_rotary_binding_routes(app,
   get_definition_store=lambda: get_definition_store(),
   get_binding_manager=lambda: get_rotary_binding_manager(),
)
register_test_recovery_routes(app, get_manager)
register_websocket_routes(app)
register_zone_routes(app,
    lambda: zigbee_service.zone_manager,
    lambda: zigbee_service.devices
)
register_ota_routes(app,
    lambda: zigbee_service.ota_manager
)
register_automation_routes(app,
    lambda: zigbee_service.automation
)
register_backup_routes(app, get_zigbee_service)
register_weather_routes(app, lambda: weather_service)
register_octopus_routes(app, lambda: octopus_service, get_zigbee_service)
register_sun_routes(app, lambda: weather_service)
register_media_routes(app, lambda: media_service)
register_cast_sync_routes(app, lambda: media_service)
register_tts_routes(app, lambda: therapy_tts)
register_heating_routes(app, lambda: heating_advisor, get_zigbee_service, lambda: heating_anomaly_watcher,
                        get_octopus=lambda: octopus_service)
register_heating_controller_routes(app, lambda: heating_controller, get_zigbee_service)
register_floor_plan_routes(app, lambda: heating_controller)
register_chamber_routes(app, get_zigbee_service)
register_frame_routes(app, get_zigbee_service)
register_ac_routes(app)
register_adblock_routes(app)
register_security_routes(app, get_matter_bridge, get_zigbee_service)
register_api_docs_routes(app)
register_wiki_routes(app)
register_alert_routes(app)
register_presence_routes(app, get_presence_manager)
from modules.journeys import get_journey_manager
from routes import register_journey_routes, register_fuel_routes
register_journey_routes(app, get_journey_manager)
register_fuel_routes(app)
from routes.message_routes import register_message_routes
from modules.messages_store import get_message_store
register_message_routes(app, get_message_store)
register_remote_access_routes(app)

# Map tiles: a caching proxy so presence maps don't announce the
# coordinates being viewed to a third-party tile server on every pan.
from routes.map_routes import register_map_routes
register_map_routes(app)

# mDNS — let the companion app find this hub and learn the PUBLIC url. A
# geofence reports when you leave home, exactly when a LAN address stops
# working, so the tunnel address is the one worth advertising.
from modules.discovery import HubAdvertiser, set_advertiser
try:
    _web_cfg = (CONFIG.get("web") or {})
    _ra_cfg = {}
    try:
        import yaml as _yaml
        from pathlib import Path as _Path
        _ra_path = _Path("./data/remote_access.yaml")
        if _ra_path.exists():
            _ra_cfg = _yaml.safe_load(_ra_path.read_text()) or {}
    except Exception:
        _ra_cfg = {}
    _public = _ra_cfg.get("hostname") or ""
    _advertiser = HubAdvertiser(
        port=int(_web_cfg.get("port", 8000)),
        public_url=(f"https://{_public}" if _public else ""),
        https=bool(_web_cfg.get("ssl", True)),
    )
    _advertiser.start()
    set_advertiser(_advertiser)
except Exception as _e:
    # Discovery is a convenience; never let it stop the hub coming up.
    logger.warning(f"[discovery] not advertising: {_e}")

# Web Push — the only channel that reaches a device with its screen off.
from modules.webpush import VapidKeys, PushManager, set_push_manager
from routes.push_routes import register_push_routes
_vapid = VapidKeys.load_or_create()
push_manager = PushManager(_vapid)
push_manager.load()
set_push_manager(push_manager)
register_push_routes(app)

async def _message_notifier(event: str, payload: dict):
    """
    Fan a message out: websocket for anyone with the app open, web push for
    the phone in a pocket. State is authoritative,
    delivery is best-effort on both channels.
    """
    try:
        await broadcast_event(event, payload)
    except Exception as e:
        logger.debug(f"[messages] websocket broadcast failed: {e}")

    if event != "message_created":
        return                        # read-receipts are not worth a wake-up
    try:
        target = payload.get("to_user")
        if not target:
            return
        await push_manager.send_to_user(target, {
            "title": payload.get("from_user") or "Message",
            "body": payload.get("body") or "",
            # One tag per thread: a burst of messages collapses into one
            # notification showing the latest, instead of a stack.
            "tag": f"zmm-msg-{payload.get('thread_id')}",
            "kind": "message_created",
            "data": {"peer": payload.get("from_user")},
        })
    except Exception as e:
        logger.warning(f"[messages] push failed: {e}")


from modules.messages_store import MessageStore, set_message_store, get_message_store
message_store = MessageStore(notifier=_message_notifier)
set_message_store(message_store)

# Named places — shared geofences beyond home. Loaded before the routes so a
# request arriving immediately after startup sees the configured set.
from modules.places import PlaceManager, set_place_manager
from routes.place_routes import register_place_routes
place_manager = PlaceManager()
place_manager.load()
set_place_manager(place_manager)
register_place_routes(app)


@app.post("/api/setup/start-services")
async def start_services_after_setup():
    """
    Called by the setup wizard after all config is applied.
    Starts MQTT (if enabled) and Zigbee, streaming probe progress via WS.
    """
    global CONFIG

    try:
        # Re-read config
        CONFIG = load_config()
        mqtt_enabled = get_conf('mqtt', 'enabled', True)

        # Step 1: MQTT
        await manager.broadcast({
            "type": "setup_phase",
            "payload": {"phase": "mqtt", "message": "Configuring MQTT..."}
        })

        if mqtt_enabled:
            mqtt_service.broker = get_conf('mqtt', 'broker_host', 'localhost')
            mqtt_service.port = get_conf('mqtt', 'broker_port', 1883)
            mqtt_service.username = get_conf('mqtt', 'username')
            mqtt_service.password = get_conf('mqtt', 'password')
            mqtt_service.base_topic = get_conf('mqtt', 'base_topic', 'zigbee_matter_manager')

            await mqtt_service.stop()
            await mqtt_service.start()

            mqtt_service.mqtt_explorer = MQTTExplorer(mqtt_service, max_messages=1000)
            async def mqtt_explorer_callback(message_record):
                await manager.broadcast({"type": "mqtt_message", "payload": message_record})
            mqtt_service.mqtt_explorer.add_callback(mqtt_explorer_callback)

            await manager.broadcast({
                "type": "setup_phase",
                "payload": {
                    "phase": "mqtt_done",
                    "message": f"MQTT connected to {mqtt_service.broker}",
                    "success": mqtt_service.connected,
                }
            })
        else:
            await manager.broadcast({
                "type": "setup_phase",
                "payload": {"phase": "mqtt_done", "message": "MQTT disabled (standalone)", "success": True}
            })

        # Step 2: Zigbee with live probe progress
        await manager.broadcast({
            "type": "setup_phase",
            "payload": {"phase": "zigbee_probe", "message": "Detecting Zigbee coordinator..."}
        })

        new_port = get_conf('zigbee', 'port', '/dev/ttyACM0')
        zigbee_service.port = new_port

        ensure_network_credentials("./config/config.yaml")
        CONFIG = load_config()
        # Assign AFTER the reload so freshly generated/imported credentials
        # (not the template placeholders) reach the radio config builder
        zigbee_service._config = CONFIG.get('zigbee', {})
        network_key = get_conf('zigbee', 'network_key', None)

        # Progress callback that broadcasts Dongle Jedi events to frontend
        async def probe_progress(progress):
            await manager.broadcast({
                "type": "setup_probe_progress",
                "payload": progress.to_dict(),
            })

        await zigbee_service.start(
            network_key=network_key,
            probe_progress_cb=probe_progress,
        )

        # Wire group callback
        if mqtt_enabled:
            mqtt_service.group_command_callback = zigbee_service.group_manager.handle_mqtt_group_command

        await manager.broadcast({
            "type": "setup_complete",
            "payload": {"message": "All services started successfully"}
        })

        return {"success": True, "message": f"Services started on {new_port}"}

    except Exception as e:
        logger.error(f"Failed to start services: {e}", exc_info=True)
        await manager.broadcast({
            "type": "setup_error",
            "payload": {"error": str(e)}
        })
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    ssl_config = CONFIG.get('web', {}).get('ssl', {}) or {}
    # HTTPS is always on: no supported HTTP mode and no UI toggle. A self-signed
    # cert is auto-generated on first boot; web.ssl only carries optional cert/key
    # path overrides. A stale `enabled: false` in an old config.yaml is ignored.

    host = get_conf('web', 'host') or '0.0.0.0'
    # Environment variable takes priority (set by build.sh for host networking),
    # then config.yaml, then default 8000
    port = int(os.environ.get('ZMM_PORT', 0)) or int(get_conf('web', 'port') or 8000)

    kwargs = {
        "app": "main:app",
        "host": host,
        "port": port,
        "log_level": (get_conf('logging', 'level') or 'info').lower(),
    }

    cert_file = ssl_config.get('cert_file', './data/certs/cert.pem')
    key_file = ssl_config.get('key_file', './data/certs/key.pem')
    from modules.ssl_bootstrap import ensure_self_signed_cert
    action = ensure_self_signed_cert(cert_file, key_file)
    if os.path.isfile(cert_file) and os.path.isfile(key_file):
        kwargs["ssl_certfile"] = cert_file
        kwargs["ssl_keyfile"] = key_file
        logger.info(f"Starting with SSL on https://{host}:{port} (cert {action})")
    else:
        # Cert generation failed and no usable pair on disk — serve HTTP rather
        # than crash-loop. The only path to HTTP, and an error condition.
        logger.error(
            f"No usable TLS certificate ({action}) — falling back to "
            f"http://{host}:{port}. Install openssl or drop a cert at {cert_file}."
        )

    uvicorn.run(**kwargs)