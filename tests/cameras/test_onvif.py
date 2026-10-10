"""
The ONVIF client (modules/onvif.py) against a fake camera that answers like
real ones do: discovery replies, digest auth, profiles, stream URIs, events.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re

from harness import Checker

from modules import onvif as O

ENV = '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" ' \
      'xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" ' \
      'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" ' \
      'xmlns:tev="http://www.onvif.org/ver10/events/wsdl" xmlns:wsa="http://www.w3.org/2005/08/addressing">' \
      '<s:Body>{}</s:Body></s:Envelope>'

PROBE_MATCH = """<?xml version="1.0"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><e:Body><d:ProbeMatches><d:ProbeMatch>
 <d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/Front%20Door
 onvif://www.onvif.org/hardware/C320WS</d:Scopes>
 <d:XAddrs>http://192.168.1.50:2020/onvif/device_service http://[fe80::1]/onvif/device_service</d:XAddrs>
 </d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>"""

TIME = ENV.format("<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime>"
                  "<tt:Date><tt:Year>2030</tt:Year><tt:Month>1</tt:Month><tt:Day>1</tt:Day></tt:Date>"
                  "<tt:Time><tt:Hour>0</tt:Hour><tt:Minute>0</tt:Minute><tt:Second>0</tt:Second></tt:Time>"
                  "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")
CAPS = ENV.format("<tds:GetCapabilitiesResponse><tds:Capabilities>"
                  "<tt:Events><tt:XAddr>http://10.9.9.9/onvif/event_service</tt:XAddr></tt:Events>"
                  "<tt:Media><tt:XAddr>http://10.9.9.9/onvif/media_service</tt:XAddr></tt:Media>"
                  "</tds:Capabilities></tds:GetCapabilitiesResponse>")
PROFILES = ENV.format('<trt:GetProfilesResponse><trt:Profiles token="main"><tt:Name>Main</tt:Name>'
                      "<tt:VideoEncoderConfiguration><tt:Resolution><tt:Width>2304</tt:Width><tt:Height>1296</tt:Height>"
                      "</tt:Resolution></tt:VideoEncoderConfiguration></trt:Profiles>"
                      '<trt:Profiles token="sub"><tt:Name>Sub</tt:Name></trt:Profiles></trt:GetProfilesResponse>')
URI = ENV.format("<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://192.168.1.50:554/{}</tt:Uri>"
                 "</trt:MediaUri></trt:GetStreamUriResponse>")
SUB = ENV.format("<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference>"
                 "<wsa:Address>http://10.9.9.9/onvif/pullpoint/7</wsa:Address></tev:SubscriptionReference>"
                 "</tev:CreatePullPointSubscriptionResponse>")


def notif(topic, name, value):
    return ("<wsnt:NotificationMessage><wsnt:Topic>" + topic + "</wsnt:Topic><wsnt:Message><tt:Message>"
            f'<tt:Data><tt:SimpleItem Name="{name}" Value="{value}"/></tt:Data></tt:Message></wsnt:Message>'
            "</wsnt:NotificationMessage>")


class FakeCam:
    def __init__(self, user="admin", password="pw"):
        self.user, self.password = user, password
        self.calls = []
        self.events = []

    def _authed(self, body):
        m = re.search(r"<Username>(.*?)</Username>.*?>([^<]+)</Password>.*?>([^<]+)</Nonce>.*?>([^<]+)</Created>", body, re.S)
        if not m:
            return False
        user, digest, nonce, created = m.groups()
        want = base64.b64encode(hashlib.sha1(base64.b64decode(nonce) + created.encode()
                                             + self.password.encode()).digest()).decode()
        self.created = created
        return user == self.user and digest == want

    async def __call__(self, url, body, headers):
        action = re.search(r'action="([^"]+)"', headers["Content-Type"]).group(1)
        self.calls.append((url, action.rsplit("/", 1)[-1]))
        if action.endswith("GetSystemDateAndTime"):
            return 200, TIME
        if not self._authed(body):
            return 400, ENV.format("<s:Fault><s:Code><s:Subcode><s:Value>ter:NotAuthorized</s:Value>"
                                   "</s:Subcode></s:Code><s:Reason><s:Text>Sender not Authorized</s:Text></s:Reason></s:Fault>")
        if action.endswith("GetCapabilities"):
            return 200, CAPS
        if action.endswith("GetProfiles"):
            return 200, PROFILES
        if action.endswith("GetStreamUri"):
            return 200, URI.format("stream1" if ">main<" in body else "stream2")
        if action.endswith("CreatePullPointSubscriptionRequest"):
            return 200, SUB
        if action.endswith("PullMessagesRequest"):
            msgs, self.events = self.events, []
            return 200, ENV.format("<tev:PullMessagesResponse>" + "".join(msgs) + "</tev:PullMessagesResponse>")
        return 200, ENV.format("<ok/>")


def run() -> Checker:
    c = Checker("onvif")

    c.section("discovery")
    m = O.parse_probe_match(PROBE_MATCH)
    c.check("a probe match gives host, port, name and model",
            m == {"xaddr": "http://192.168.1.50:2020/onvif/device_service", "host": "192.168.1.50",
                  "port": 2020, "name": "Front Door", "hardware": "C320WS"}, m)
    c.check("something that isn't a camera is ignored", O.parse_probe_match("<x/>") is None)
    c.check("a reply declaring entities is refused",
            O.parse_probe_match('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>') is None)

    async def scenario():
        c.section("media")
        fake = FakeCam()
        cam = O.OnvifCamera("192.168.1.50", 2020, "admin", "pw", post=fake)
        profiles = await cam.profiles()
        c.check("profiles come back with their RTSP URIs and sizes",
                profiles == [{"token": "main", "name": "Main", "width": 2304, "height": 1296,
                              "uri": "rtsp://192.168.1.50:554/stream1"},
                             {"token": "sub", "name": "Sub", "width": 0, "height": 0,
                              "uri": "rtsp://192.168.1.50:554/stream2"}], profiles)
        c.check("the digest is signed on the camera's clock, not ours",
                fake.created.startswith("2030-01-01T00:0"), getattr(fake, "created", None))
        c.check("services the camera advertises on another address are reached at the one we know",
                all(u.startswith("http://192.168.1.50:2020/") for u, _ in fake.calls), fake.calls)

        bad = O.OnvifCamera("192.168.1.50", 2020, "admin", "wrong", post=FakeCam())
        try:
            await bad.profiles()
            c.check("a wrong password says so", False)
        except O.OnvifError as e:
            c.check("a wrong password says so", "username or password" in str(e), str(e))

        async def silent(url, body, headers):
            raise ConnectionRefusedError()
        try:
            await O.OnvifCamera("192.168.1.99", 80, post=silent).profiles()
            c.check("an unreachable camera says so", False)
        except O.OnvifError as e:
            c.check("an unreachable camera says so", "no answer" in str(e), str(e))

        async def huge(url, body, headers):
            return 200, "<x>" + "a" * (O.MAX_REPLY + 10) + "</x>"
        try:
            await O.OnvifCamera("h", 80, post=huge).sync_clock()
            c.check("an oversized reply is refused", False)
        except O.OnvifError:
            c.check("an oversized reply is refused", True)

        c.section("events")
        addr = await cam.subscribe()
        c.check("a pull point is created and reached at the known address",
                addr == "http://192.168.1.50:2020/onvif/pullpoint/7", addr)
        fake.events = [notif("tns1:RuleEngine/CellMotionDetector/Motion", "IsMotion", "true"),
                       notif("tns1:VideoSource/MotionAlarm", "State", "false"),
                       notif("tns1:Device/Trigger/DigitalInput", "LogicalState", "true")]
        evs = await cam.pull(addr)
        c.check("motion on and off are read across vendors' topics; other events ignored",
                [e["motion"] for e in evs] == [True, False], evs)
        c.check("pulls carry WS-Addressing, which many cameras require",
                ("http://192.168.1.50:2020/onvif/pullpoint/7", "PullMessagesRequest") in fake.calls)

    asyncio.run(scenario())
    return c
