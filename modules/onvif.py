"""
Just enough ONVIF for cameras: WS-Discovery, stream URIs, and motion events
over a pull-point subscription. Standard library plus httpx; no zeep.
See docs/cameras.md §ONVIF.

Cameras sign requests with a WS-UsernameToken digest over their own clock, and
many refuse one more than a few seconds out, so the offset from
GetSystemDateAndTime (which needs no auth) is applied to every `Created`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse
from xml.sax.saxutils import escape

logger = logging.getLogger("onvif")

NS = {
    "env": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wsa": "http://www.w3.org/2005/08/addressing",
}
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
B64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"

DISCOVERY_ADDR = ("239.255.255.250", 3702)
# Cameras answer in a few kB; anything far bigger is not a camera.
MAX_REPLY = 256 * 1024
TIMEOUT_S = 8

PostFn = Callable[[str, str, Dict[str, str]], Awaitable[Tuple[int, str]]]


class OnvifError(Exception):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_all(root: ET.Element, name: str) -> List[ET.Element]:
    return [e for e in root.iter() if _local(e.tag) == name]


def _find(root: ET.Element, name: str) -> Optional[ET.Element]:
    return next(iter(_find_all(root, name)), None)


def _text(root: Optional[ET.Element], name: str) -> str:
    e = _find(root, name) if root is not None else None
    return (e.text or "").strip() if e is not None else ""


def _parse(text: str) -> ET.Element:
    if len(text) > MAX_REPLY:
        raise OnvifError("reply too large")
    if "<!DOCTYPE" in text[:2000] or "<!ENTITY" in text:
        raise OnvifError("reply declares a DTD")      # no entity games
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise OnvifError(f"unreadable reply: {e}") from e


# Discovery

PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:{id}</w:MessageID>
<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"""


def parse_probe_match(text: str) -> Optional[Dict[str, Any]]:
    try:
        root = _parse(text)
    except OnvifError:
        return None
    xaddrs = _text(root, "XAddrs").split()
    if not xaddrs:
        return None
    scopes = _text(root, "Scopes").split()

    def scope(kind: str) -> str:
        from urllib.parse import unquote
        for s in scopes:
            if s.startswith(f"onvif://www.onvif.org/{kind}/"):
                return unquote(s.split("/", 4)[-1])
        return ""
    u = urlparse(xaddrs[0])
    return {"xaddr": xaddrs[0], "host": u.hostname or "", "port": u.port or 80,
            "name": scope("name"), "hardware": scope("hardware")}


def _discover_blocking(timeout: float) -> List[Dict[str, Any]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    found: Dict[str, Dict[str, Any]] = {}
    try:
        sock.sendto(PROBE.format(id=uuid.uuid4()).encode(), DISCOVERY_ADDR)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            m = parse_probe_match(data.decode("utf-8", "replace"))
            if m and m["host"] and m["host"] not in found:
                found[m["host"]] = m
    finally:
        sock.close()
    return sorted(found.values(), key=lambda m: m["host"])


async def discover(timeout: float = 3.0) -> List[Dict[str, Any]]:
    """Cameras answering a WS-Discovery probe on this host's LAN."""
    return await asyncio.to_thread(_discover_blocking, timeout)


# SOAP

async def _httpx_post(url: str, body: str, headers: Dict[str, str]) -> Tuple[int, str]:
    import httpx
    async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False) as cx:
        r = await cx.post(url, content=body.encode(), headers=headers)
    return r.status_code, r.text[:MAX_REPLY + 1]


class OnvifCamera:
    """One camera's ONVIF services, addressed at the host:port it was added with."""

    def __init__(self, host: str, port: int, username: str = "", password: str = "",
                 post: Optional[PostFn] = None):
        self.host, self.port = host, int(port or 80)
        self.username, self.password = username, password
        self._post = post or _httpx_post
        self._offset = 0.0            # camera clock minus ours
        self._xaddrs: Dict[str, str] = {}

    @property
    def device_url(self) -> str:
        return f"http://{self.host}:{self.port}/onvif/device_service"

    def _rehost(self, xaddr: str) -> str:
        # Cameras behind NAT or with two NICs advertise addresses we can't
        # reach; keep their path, use the address we can.
        u = urlparse(xaddr)
        return urlunparse(u._replace(scheme="http", netloc=f"{self.host}:{self.port}"))

    def _security(self) -> str:
        if not self.username:
            return ""
        nonce = os.urandom(16)
        created = datetime.fromtimestamp(time.time() + self._offset, timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode())
                                  .digest()).decode()
        return (f'<Security s:mustUnderstand="1" xmlns="{WSSE}"><UsernameToken>'
                f"<Username>{escape(self.username)}</Username>"
                f'<Password Type="{DIGEST}">{digest}</Password>'
                f'<Nonce EncodingType="{B64}">{base64.b64encode(nonce).decode()}</Nonce>'
                f'<Created xmlns="{WSU}">{created}</Created></UsernameToken></Security>')

    async def call(self, url: str, action: str, body: str, auth: bool = True,
                   addressing: bool = False) -> ET.Element:
        header = self._security() if auth else ""
        if addressing:
            header += (f'<a:Action xmlns:a="{NS["wsa"]}">{escape(action)}</a:Action>'
                       f'<a:To xmlns:a="{NS["wsa"]}">{escape(url)}</a:To>')
        env = (f'<?xml version="1.0" encoding="UTF-8"?>'
               f'<s:Envelope xmlns:s="{NS["env"]}" xmlns:tds="{NS["tds"]}" xmlns:trt="{NS["trt"]}" '
               f'xmlns:tt="{NS["tt"]}" xmlns:tev="{NS["tev"]}" xmlns:wsnt="{NS["wsnt"]}">'
               f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>")
        headers = {"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'}
        try:
            status, text = await self._post(url, env, headers)
        except Exception as e:                            # noqa: BLE001
            raise OnvifError(f"no answer from {self.host}: {type(e).__name__}") from e
        root = _parse(text) if text.strip() else None
        if status >= 400 or root is None:
            reason = (_text(root, "Text") or _text(root, "Value")) if root is not None else ""
            if status == 401 or "notauthorized" in reason.lower().replace(" ", ""):
                raise OnvifError("the camera refused the username or password")
            raise OnvifError(f"camera answered {status}{': ' + reason if reason else ''}")
        return root

    async def sync_clock(self) -> None:
        root = await self.call(self.device_url, f"{NS['tds']}/GetSystemDateAndTime",
                               "<tds:GetSystemDateAndTime/>", auth=False)
        utc = _find(root, "UTCDateTime")
        if utc is None:
            return
        try:
            d, t = _find(utc, "Date"), _find(utc, "Time")
            cam = datetime(int(_text(d, "Year")), int(_text(d, "Month")), int(_text(d, "Day")),
                           int(_text(t, "Hour")), int(_text(t, "Minute")), int(_text(t, "Second")),
                           tzinfo=timezone.utc).timestamp()
            self._offset = cam - time.time()
        except (TypeError, ValueError):
            pass

    async def services(self) -> Dict[str, str]:
        if self._xaddrs:
            return self._xaddrs
        await self.sync_clock()
        root = await self.call(self.device_url, f"{NS['tds']}/GetCapabilities",
                               "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
        for svc in ("Media", "Events"):
            el = _find(root, svc)
            x = _text(el, "XAddr") if el is not None else ""
            if x:
                self._xaddrs[svc.lower()] = self._rehost(x)
        if "media" not in self._xaddrs:
            raise OnvifError("the camera has no ONVIF media service")
        return self._xaddrs

    async def profiles(self) -> List[Dict[str, Any]]:
        """Each profile with its RTSP URI, credentials not included."""
        media = (await self.services())["media"]
        root = await self.call(media, f"{NS['trt']}/GetProfiles", "<trt:GetProfiles/>")
        out = []
        for p in _find_all(root, "Profiles"):
            token = p.get("token") or ""
            if not token:
                continue
            res = _find(p, "Resolution")
            setup = ("<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                     "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                     f"<trt:ProfileToken>{escape(token)}</trt:ProfileToken></trt:GetStreamUri>")
            try:
                uri_root = await self.call(media, f"{NS['trt']}/GetStreamUri", setup)
                uri = _text(uri_root, "Uri")
            except OnvifError as e:
                logger.debug("[onvif] %s profile %s: %s", self.host, token, e)
                uri = ""
            out.append({"token": token, "name": _text(p, "Name") or token,
                        "width": int(_text(res, "Width") or 0) if res is not None else 0,
                        "height": int(_text(res, "Height") or 0) if res is not None else 0,
                        "uri": uri})
        return out

    # Events
    async def subscribe(self) -> str:
        events = (await self.services()).get("events")
        if not events:
            raise OnvifError("the camera has no ONVIF events service")
        root = await self.call(
            events, f"{NS['tev']}/EventPortType/CreatePullPointSubscriptionRequest",
            "<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT120S"
            "</tev:InitialTerminationTime></tev:CreatePullPointSubscription>", addressing=True)
        ref = _find(root, "SubscriptionReference")
        addr = _text(ref, "Address") if ref is not None else ""
        if not addr:
            raise OnvifError("the camera gave no pull-point address")
        return self._rehost(addr)

    async def pull(self, address: str, wait_s: int = 10) -> List[Dict[str, Any]]:
        root = await self.call(
            address, f"{NS['tev']}/PullPointSubscription/PullMessagesRequest",
            f"<tev:PullMessages><tev:Timeout>PT{int(wait_s)}S</tev:Timeout>"
            f"<tev:MessageLimit>20</tev:MessageLimit></tev:PullMessages>", addressing=True)
        return parse_notifications(root)

    async def renew(self, address: str) -> None:
        await self.call(address, "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest",
                        "<wsnt:Renew><wsnt:TerminationTime>PT120S</wsnt:TerminationTime></wsnt:Renew>",
                        addressing=True)

    async def unsubscribe(self, address: str) -> None:
        try:
            await self.call(address, "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest",
                            "<wsnt:Unsubscribe/>", addressing=True)
        except OnvifError:
            pass


# Topics vary by vendor (RuleEngine/CellMotionDetector/Motion,
# VideoSource/MotionAlarm, ...); all name motion. The flag's item name varies too.
MOTION_ITEMS = ("ismotion", "state", "motion", "motionactive")


def parse_notifications(root: ET.Element) -> List[Dict[str, Any]]:
    """Motion on/off events in a PullMessages reply."""
    out = []
    for msg in _find_all(root, "NotificationMessage"):
        if "motion" not in _text(msg, "Topic").lower():
            continue
        data = _find(msg, "Data")
        for item in _find_all(data, "SimpleItem") if data is not None else []:
            if (item.get("Name") or "").lower() in MOTION_ITEMS:
                out.append({"topic": _text(msg, "Topic"),
                            "motion": (item.get("Value") or "").lower() in ("true", "1")})
                break
    return out
