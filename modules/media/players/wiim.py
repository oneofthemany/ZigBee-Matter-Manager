"""
WiiM / LinkPlay player provider.

Talks the documented WiiM HTTP API (``/httpapi.asp?command=...``). The core
transport/volume/status commands are from WiiM's official HTTP API PDF. The
multiroom (grouping) commands are LinkPlay-platform commands that are NOT in
that PDF — they're community-documented and semi-official, so they're isolated
here and degrade gracefully if a device rejects them.

Phase 1 discovery is a manual list of device IPs from config. mDNS discovery
(LinkPlay advertises ``_linkplay._tcp`` / UPnP) is a later enhancement.

Transport note: newer WiiM firmware serves the API over HTTPS (port 443) with a
self-signed cert and may disable plain HTTP. We probe per-device once and cache
the working scheme; HTTPS uses verify=False (the cert is the device's own).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

import httpx

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

    def __init__(self, device_ips: List[str], enabled: bool = True):
        self.enabled = enabled
        self._ips: List[str] = list(device_ips or [])
        # Cached scheme ("https" | "http") per IP, learned on first contact.
        self._scheme: Dict[str, str] = {}
        # Cached display names from getStatusEx.
        self._names: Dict[str, str] = {}
        # EQ preset list per IP (None = not probed yet, [] = unsupported) and
        # the last preset WE loaded — the API can report on/off (EQGetStat)
        # but not which preset is active, so we remember our own writes.
        self._eq_presets: Dict[str, Optional[List[str]]] = {}
        self._eq_current: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Discovery / state
    # ------------------------------------------------------------------
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

    async def get_state(self, player_id: str) -> Optional[PlayerState]:
        ip = player_id.split(":", 1)[1]
        if ip not in self._ips:
            return None

        # Name (cached after first lookup).
        if ip not in self._names:
            ex = await self._command_json(ip, "getStatusEx")
            if ex:
                self._names[ip] = ex.get("DeviceName") or ex.get("ssid") or ip

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

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Equaliser (official WiiM HTTP API: EQOn/EQOff/EQGetList/EQLoad/EQGetStat)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Native multiroom (LinkPlay — semi-official, not in WiiM HTTP PDF)
    # ------------------------------------------------------------------
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
