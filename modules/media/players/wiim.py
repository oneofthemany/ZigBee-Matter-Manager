"""
WiiM / LinkPlay player provider, over the documented /httpapi.asp API.

Multiroom grouping commands are community-documented LinkPlay extensions rather
than official, so they are isolated here and degrade gracefully. Newer firmware
may serve only HTTPS with a self-signed cert, so the scheme is probed once per
device and cached. See docs/speaker_sync.md.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

import httpx

from modules.media import linkplay as lp
from modules.media.models import MediaItem, PlayerState, PlaybackState
from modules.media.players.base import PlayerProvider

logger = logging.getLogger("modules.media.wiim")

# WiiM getPlayerStatus "status" -> our PlaybackState
_STATE_MAP = {
    "play": PlaybackState.PLAYING,
    "load": PlaybackState.BUFFERING,
    "pause": PlaybackState.PAUSED,
    "stop": PlaybackState.IDLE,
    "none": PlaybackState.IDLE,
}


def _decode_hex(value: str) -> str:
    """WiiM encodes Title/Artist/Album as hex UTF-8. Fall back to raw text."""
    if not value:
        return ""
    try:
        return bytes.fromhex(value).decode("utf-8", errors="ignore").strip()
    except ValueError:
        return value.strip()


def _pid(ip: str) -> str:
    return f"wiim:{ip}"


class WiiMPlayerProvider(PlayerProvider):
    provider = "wiim"
    label = "WiiM"
    #: LinkPlay multiroom — the master syncs its slaves in firmware.
    groups_natively = True
    #: inputs, presets, output, sleep timer — see device_panel
    has_device_panel = True

    def __init__(self, device_ips: List[str], enabled: bool = True):
        self.enabled = enabled
        self._ips: List[str] = list(device_ips or [])
        # Cached scheme ("https" | "http") per IP, learned on first contact.
        self._scheme: Dict[str, str] = {}
        # Cached display names from getStatusEx.
        self._names: Dict[str, str] = {}
        # Cached hardware identity from getStatusEx ("project"), for model_key.
        self._models: Dict[str, str] = {}
        # EQ preset list per IP (None = not probed yet, [] = unsupported) and
        # the last preset WE loaded — the API can report on/off (EQGetStat)
        # but not which preset is active, so we remember our own writes.
        self._eq_presets: Dict[str, Optional[List[str]]] = {}
        self._eq_current: Dict[str, str] = {}

    def add_device(self, ip: str, ident: Optional[dict] = None) -> None:
        """Adopt a discovered device (LinkPlayDirectory.on_found)."""
        if ip in self._ips:
            return
        self._ips.append(ip)
        if ident:
            if ident.get("name"):
                self._names[ip] = ident["name"]
            if ident.get("project"):
                self._models[ip] = ident["project"]

    # HTTP plumbing
    async def _command(self, ip: str, command: str) -> Optional[str]:
        """Issue one httpapi command. Returns raw text body, or None on failure."""
        schemes = [self._scheme[ip]] if ip in self._scheme else ["https", "http"]
        for scheme in schemes:
            url = f"{scheme}://{ip}/httpapi.asp"
            try:
                async with httpx.AsyncClient(timeout=6.0, verify=False) as client:
                    resp = await client.get(url, params={"command": command})
                    resp.raise_for_status()
                    self._scheme[ip] = scheme  # remember what worked
                    return resp.text
            except httpx.HTTPError as e:
                logger.debug(f"WiiM {ip} {scheme} '{command}' failed: {e}")
                continue
        logger.warning(f"WiiM {ip}: command '{command}' unreachable")
        return None

    async def _command_json(self, ip: str, command: str) -> Optional[dict]:
        text = await self._command(ip, command)
        if not text:
            return None
        try:
            import json
            return json.loads(text)
        except ValueError:
            return None

    # Discovery / state
    async def list_players(self) -> List[PlayerState]:
        if not self.enabled or not self._ips:
            return []
        states = await asyncio.gather(
            *(self.get_state(_pid(ip)) for ip in self._ips),
            return_exceptions=True,
        )
        out: List[PlayerState] = []
        for ip, st in zip(self._ips, states):
            if isinstance(st, PlayerState):
                out.append(st)
            else:
                # Unreachable — surface as an unavailable player so the UI shows it.
                out.append(PlayerState(
                    player_id=_pid(ip),
                    provider=self.provider,
                    name=self._names.get(ip, ip),
                    available=False,
                    state=PlaybackState.UNKNOWN,
                ))
        return out

    def device_key(self, player_id: str) -> str:
        """The IP, which is already this provider's whole notion of a device
        and is what a Cast discovery of the same box reports as its host."""
        return player_id.split(":", 1)[-1].strip()

    def model_key(self, player_id: str) -> str:
        """LinkPlay's ``project`` — the firmware's own name for the hardware
        (e.g. ``WiiM_Pro_with_gc4a``). Constant for the life of the unit, and
        carried by every device on the platform, where ``DeviceName`` is
        user-set and the IP in ``player_id`` moves with the DHCP lease.

        Populated by the first ``get_state``; "" until then, which reads as
        "no model default" rather than as a wrong one.
        """
        return self._models.get(player_id.split(":", 1)[-1], "")

    async def get_state(self, player_id: str) -> Optional[PlayerState]:
        ip = player_id.split(":", 1)[1]
        if ip not in self._ips:
            return None

        # Name and hardware identity (one probe, cached after first lookup).
        if ip not in self._names:
            ex = await self._command_json(ip, "getStatusEx")
            if ex:
                self._names[ip] = ex.get("DeviceName") or ex.get("ssid") or ip
                project = (ex.get("project") or "").strip()
                if project:
                    self._models[ip] = project

        status = await self._command_json(ip, "getPlayerStatus")
        name = self._names.get(ip, ip)
        if not status:
            return PlayerState(
                player_id=player_id, provider=self.provider, name=name,
                available=False, state=PlaybackState.UNKNOWN,
            )

        try:
            vol = int(status.get("vol", 0)) / 100.0
        except (TypeError, ValueError):
            vol = 0.0
        try:
            position = int(status.get("curpos", 0))
            duration = int(status.get("totlen", 0))
        except (TypeError, ValueError):
            position = duration = 0

        # Group role: getPlayerStatus reports "group" (0/1). A master also has
        # a slave list; we fetch members lazily only when grouped.
        is_group = str(status.get("group", "0")) not in ("0", "", "none")
        members: List[str] = []
        if is_group:
            members = await self._slave_member_ids(ip)

        # WiiM has no idle_reason; flag "ended" when position reaches the track
        # length (only meaningful for finite tracks, not live radio totlen==0).
        # The controller's state-transition check is the safety net if a poll
        # misses this window.
        ended = duration > 0 and position >= duration - 3000

        return PlayerState(
            player_id=player_id,
            provider=self.provider,
            name=name,
            available=True,
            state=_STATE_MAP.get(str(status.get("status", "")).lower(), PlaybackState.UNKNOWN),
            volume=vol,
            muted=str(status.get("mute", "0")) == "1",
            is_group=is_group,
            group_members=members,
            title=_decode_hex(status.get("Title", "")),
            artist=_decode_hex(status.get("Artist", "")),
            media_type="radio",
            ended=ended,
            position_ms=position,
            duration_ms=duration,
        )

    async def _slave_member_ids(self, master_ip: str) -> List[str]:
        data = await self._command_json(master_ip, "multiroom:getSlaveList")
        if not data:
            return []
        out: List[str] = []
        for slave in data.get("slave_list", []) or []:
            sip = slave.get("ip")
            if sip:
                out.append(_pid(sip))
        return out

    # Playback
    async def play_url(self, player_id: str, item: MediaItem) -> None:
        ip = player_id.split(":", 1)[1]
        await self._command(ip, f"setPlayerCmd:play:{item.url}")

    async def pause(self, player_id: str) -> None:
        ip = player_id.split(":", 1)[1]
        await self._command(ip, "setPlayerCmd:pause")

    async def resume(self, player_id: str) -> None:
        ip = player_id.split(":", 1)[1]
        await self._command(ip, "setPlayerCmd:resume")

    async def stop_playback(self, player_id: str) -> None:
        ip = player_id.split(":", 1)[1]
        await self._command(ip, "setPlayerCmd:stop")

    async def next_track(self, player_id: str) -> None:
        ip = player_id.split(":", 1)[1]
        await self._command(ip, "setPlayerCmd:next")

    async def prev_track(self, player_id: str) -> None:
        ip = player_id.split(":", 1)[1]
        await self._command(ip, "setPlayerCmd:prev")

    async def set_volume(self, player_id: str, level: float) -> None:
        ip = player_id.split(":", 1)[1]
        vol = max(0, min(100, round(level * 100)))
        await self._command(ip, f"setPlayerCmd:vol:{vol}")

    async def set_muted(self, player_id: str, muted: bool) -> None:
        ip = player_id.split(":", 1)[1]
        await self._command(ip, f"setPlayerCmd:mute:{1 if muted else 0}")

    # Equaliser (official WiiM HTTP API: EQOn/EQOff/EQGetList/EQLoad/EQGetStat)
    async def _eq_preset_list(self, ip: str) -> List[str]:
        """Preset names from the device, cached. [] means EQ unsupported
        (older LinkPlay firmware answers 'unknown command')."""
        cached = self._eq_presets.get(ip)
        if cached is not None:
            return cached
        data = await self._command_json(ip, "EQGetList")
        presets = [str(p) for p in data] if isinstance(data, list) else []
        self._eq_presets[ip] = presets
        return presets

    async def eq_info(self, player_id: str) -> Optional[dict]:
        ip = player_id.split(":", 1)[1]
        presets = await self._eq_preset_list(ip)
        if not presets:
            return None
        stat = await self._command_json(ip, "EQGetStat")
        enabled = bool(stat) and str(stat.get("EQStat", "")).lower() == "on"
        return {
            "mode": "presets",
            "presets": presets,
            "enabled": enabled,
            "preset": self._eq_current.get(ip, ""),
        }

    async def set_eq(self, player_id: str, enabled: Optional[bool] = None,
                     preset: Optional[str] = None) -> None:
        ip = player_id.split(":", 1)[1]
        if preset:
            allowed = await self._eq_preset_list(ip)
            if preset not in allowed:
                raise ValueError(f"Unknown EQ preset '{preset}'")
            # EQLoad enables the EQ as a side effect on current firmware, but
            # that isn't documented — send EQOn explicitly unless turning off.
            if enabled is not False:
                await self._command(ip, "EQOn")
            if await self._command(ip, f"EQLoad:{preset}") is None:
                raise RuntimeError(f"EQ preset load failed on {ip}")
            self._eq_current[ip] = preset
        if enabled is not None and not (preset and enabled):
            cmd = "EQOn" if enabled else "EQOff"
            if await self._command(ip, cmd) is None:
                raise RuntimeError(f"{cmd} failed on {ip}")

    # Device panel (HTTP API v1.2 §2.1, §2.3, §2.5, §2.7–2.10)
    def _ip(self, player_id: str) -> str:
        ip = player_id.split(":", 1)[-1]
        if ip not in self._ips:
            raise ValueError(f"Unknown WiiM {player_id}")
        return ip

    async def device_panel(self, player_id: str) -> Optional[dict]:
        """Everything the box will say about itself, in one round of reads.
        A read the firmware refuses leaves its section out rather than
        failing the panel — older units lack the output and preset calls."""
        ip = self._ip(player_id)
        ex, st, meta, presets, out, sleep = await asyncio.gather(
            self._command_json(ip, "getStatusEx"),
            self._command_json(ip, "getPlayerStatus"),
            self._command_json(ip, "getMetaInfo"),
            self._command_json(ip, "getPresetInfo"),
            self._command_json(ip, "getNewAudioOutputHardwareMode"),
            self._command(ip, "getShutdown"),
        )
        if not ex and not st:
            raise RuntimeError(f"WiiM {ip} is not answering")
        ex, st = ex or {}, st or {}
        panel: dict = {"provider": self.provider, "player_id": player_id}
        eth = (ex.get("eth0") or ex.get("eth2") or "").strip()
        wired = bool(eth) and eth != "0.0.0.0"
        ssid = _decode_hex(ex.get("essid", ""))
        network = ("Ethernet" if wired else
                   f"Wi-Fi{' ' + ssid if ssid else ''} (RSSI {ex.get('RSSI', '?')} dBm)")
        panel["device"] = {
            "name": ex.get("DeviceName") or self._names.get(ip, ip),
            "model": (ex.get("project") or "").replace("_", " "),
            "firmware": ex.get("firmware", ""),
            "release": ex.get("Release", ""),
            "ip": ip,
            "mac": ex.get("MAC", ""),
            "network": network,
            "internet": str(ex.get("internet", "")) == "1",
            "update": str(ex.get("VersionUpdate", "0")) == "1",
            "new_version": ex.get("NewVer", "") if str(ex.get("NewVer", "0")) != "0" else "",
        }
        try:
            mode = int(st.get("mode"))
        except (TypeError, ValueError):
            mode = None
        panel["input"] = {
            "mode": mode,
            "current": lp.input_id(mode),
            "owner": lp.mode_owner(mode) or "",
            "options": lp.supported_inputs(ex.get("plm_support")),
        }
        try:
            loop = int(st.get("loop"))
        except (TypeError, ValueError):
            loop = None
        try:
            pos, dur = int(st.get("curpos", 0)), int(st.get("totlen", 0))
        except (TypeError, ValueError):
            pos = dur = 0
        panel["playback"] = {
            "status": str(st.get("status", "")),
            "loop": loop,
            "loop_options": [{"id": k, "label": v} for k, v in lp.LOOP_MODES.items()],
            "position_ms": pos, "duration_ms": dur,
            "can_seek": dur > 0,
        }
        md = (meta or {}).get("metaData") or {}
        md = {k.strip(): v for k, v in md.items()}      # the PDF's keys carry spaces
        clean = lambda v: "" if str(v).lower() in ("", "unknow", "unknown") else str(v)
        panel["audio"] = {
            "title": clean(md.get("title", "")),
            "artist": clean(md.get("artist", "")),
            "album": clean(md.get("album", "")),
            "artwork_url": clean(md.get("albumArtURI", "")),
            "sample_rate": clean(md.get("sampleRate", "")),
            "bit_depth": clean(md.get("bitDepth", "")),
            "bit_rate": clean(md.get("bitRate", "")),
        }
        if presets is not None:
            try:
                slots = int(ex.get("preset_key") or 12)
            except (TypeError, ValueError):
                slots = 12
            panel["presets"] = {
                "slots": slots,
                "items": [{"number": int(p.get("number", 0)),
                           "name": p.get("name", ""),
                           "source": p.get("source", ""),
                           "artwork_url": p.get("picurl", "")}
                          for p in presets.get("preset_list") or []
                          if str(p.get("number", "")).isdigit()],
            }
        if out and str(out.get("hardware", "")).isdigit():
            panel["output"] = {
                "current": int(out["hardware"]),
                "options": [{"id": k, "label": v} for k, v in lp.OUTPUTS.items()],
                "bt_source": str(out.get("source", "0")) == "1",
            }
        try:
            panel["sleep"] = {"seconds": max(0, int(str(sleep).strip()))}
        except (TypeError, ValueError):
            pass
        return panel

    async def device_action(self, player_id: str, action: str, value=None) -> None:
        """One panel action. Values are checked against what the box offers,
        so nothing but documented commands reaches it."""
        ip = self._ip(player_id)
        if action == "input":
            ex = await self._command_json(ip, "getStatusEx") or {}
            allowed = {i["id"] for i in lp.supported_inputs(ex.get("plm_support"))}
            if value not in allowed:
                raise ValueError(f"{value!r} is not an input this WiiM has")
            cmd = f"setPlayerCmd:switchmode:{value}"
        elif action == "preset":
            n = int(value)
            if not 1 <= n <= 12:
                raise ValueError("Preset must be 1–12")
            cmd = f"MCUKeyShortClick:{n}"
        elif action == "loop":
            n = int(value)
            if n not in lp.LOOP_MODES:
                raise ValueError("Unknown loop mode")
            cmd = f"setPlayerCmd:loopmode:{n}"
        elif action == "seek":
            cmd = f"setPlayerCmd:seek:{max(0, int(float(value)))}"
        elif action == "output":
            n = int(value)
            if n not in lp.OUTPUTS:
                raise ValueError("Unknown output")
            cmd = f"setAudioOutputHardwareMode:{n}"
        elif action == "sleep":
            n = int(value)
            if n != -1 and not 1 <= n <= 24 * 3600:
                raise ValueError("Sleep timer must be 1 s–24 h, or -1 to cancel")
            cmd = f"setShutdown:{n}"
        elif action == "toggle":
            cmd = "setPlayerCmd:onepause"
        elif action == "reboot":
            cmd = "reboot"
        else:
            raise ValueError(f"Unknown WiiM action '{action}'")
        reply = await self._command(ip, cmd)
        if reply is None or reply.strip().strip('"').lower() in ("failed", "fail"):
            raise RuntimeError(f"WiiM refused {action}"
                               f"{f' {value}' if value is not None else ''}")

    # Native multiroom (LinkPlay — semi-official, not in WiiM HTTP PDF)
    async def join_group(self, master_id: str, member_ids: List[str]) -> None:
        master_ip = master_id.split(":", 1)[1]
        for member_id in member_ids:
            slave_ip = member_id.split(":", 1)[1]
            # The join command is issued to the SLAVE, pointing at the master.
            ok = await self._command(
                slave_ip,
                f"ConnectMasterAp:JoinGroupMaster:eth{master_ip}:wifi0.0.0.0",
            )
            if ok is None:
                logger.warning(f"WiiM group: {slave_ip} failed to join {master_ip}")

    async def ungroup(self, master_id: str) -> None:
        master_ip = master_id.split(":", 1)[1]
        # Issued to the master: dissolve the whole group.
        await self._command(master_ip, "multiroom:Ungroup")
