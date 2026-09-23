"""
Full device probe — walks every endpoint and cluster a device reports and
records what it actually answers, independent of our handlers and of zigpy's
attribute cache.

Every frame the device sends while the probe runs is captured raw (via the
service's handle_message tap) and decoded by the small ZCL parser below, so a
decode/scaling bug anywhere upstream shows up as a disagreement between the
raw bytes and zigpy's view. Read-only: nothing is written to the device and
the stored model is not changed.
"""
import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

import zigpy.zcl
from zigpy.zcl import foundation

from modules.zcl_decode import parse_zcl, type_name as _tname

logger = logging.getLogger("modules.device_probe")

PROBE_DIR = "./data/probes"
STEP_TIMEOUT = 6.0
READ_CHUNK = 3

class DeviceProbe:
    def __init__(self, service, ieee: str, listen_s: int = 60,
                 emit: Optional[Callable[[str, str], None]] = None):
        self.svc = service
        self.ieee = ieee
        self.listen_s = max(0, min(int(listen_s), 600))
        self._emit = emit or (lambda level, msg: None)
        self.wrapper = service.devices[ieee]
        self.zdev = self.wrapper.zigpy_dev
        self.frames: List[Dict[str, Any]] = []
        self._phase = "setup"
        self.report: Dict[str, Any] = {
            "ieee": ieee,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": str(self.zdev.model), "manufacturer": str(self.zdev.manufacturer),
            "zigpy_class": type(self.zdev).__name__,
            "nwk": f"0x{self.zdev.nwk:04X}",
            "endpoints": {}, "frames": self.frames, "errors": [],
        }

    # ---- plumbing -------------------------------------------------------

    def log(self, level: str, msg: str):
        getattr(logger, level.lower())(f"[{self.ieee}] probe: {msg}")
        self._emit(level, msg)

    def tap(self, profile: int, cluster: int, src_ep: int, dst_ep: int, message: bytes,
            direction: str = "RX"):
        """Called for every frame to/from this device: RX from service.handle_message,
        TX from the send_packet tap. Pair a request with its reply by tsn."""
        try:
            rec = {"t": round(time.time(), 3), "dir": direction, "phase": self._phase,
                   "profile": f"0x{profile:04X}", "cluster": f"0x{cluster:04X}",
                   "src_ep": src_ep, "dst_ep": dst_ep}
            rec.update(parse_zcl(bytes(message)) if profile != 0
                       else {"hex": bytes(message).hex(), "tsn": message[0] if message else None})
            self.frames.append(rec)
            if self._phase == "listen" and profile != 0 and direction == "RX":
                vals = ", ".join(f"{r['attr']}={r.get('value', r.get('status'))}"
                                 for r in rec.get("records", []))
                self.log("INFO", f"RX EP{src_ep} 0x{cluster:04X} {rec.get('command')} "
                                 f"{vals or rec.get('payload', '')}  raw={rec['hex']}")
        except Exception as e:
            logger.debug(f"[{self.ieee}] probe tap error: {e}")

    async def _req(self, label: str, coro):
        try:
            return await asyncio.wait_for(coro, timeout=STEP_TIMEOUT)
        except asyncio.TimeoutError:
            self.report["errors"].append(f"{label}: timeout")
            self.log("WARNING", f"{label}: timed out")
        except Exception as e:
            self.report["errors"].append(f"{label}: {e!r}")
            self.log("WARNING", f"{label}: {e!r}")
        return None

    @staticmethod
    def _hex(ids) -> str:
        return "[" + ", ".join(f"0x{c:04X}" for c in sorted(ids)) + "]"

    # ---- probe steps ----------------------------------------------------

    async def run(self) -> Dict[str, Any]:
        z = self.zdev
        self.log("INFO", f"FULL PROBE start — model={z.model!r} manufacturer={z.manufacturer!r} "
                         f"nwk=0x{z.nwk:04X} class={type(z).__name__}")
        self._phase = "zdo"
        mfr_code = None
        r = await self._req("Node_Desc", z.zdo.Node_Desc_req(z.nwk))
        if r and r[0] == 0:
            nd = r[2]
            mfr_code = int(nd.manufacturer_code)
            self.report["node_descriptor"] = str(nd)
            self.log("INFO", f"Node Descriptor: manufacturer_code=0x{mfr_code:04X} "
                             f"type={nd.logical_type!r} mac={nd.mac_capability_flags!r}")

        eps = sorted(ep for ep in z.endpoints if ep != 0)
        r = await self._req("Active_EP", z.zdo.Active_EP_req(z.nwk))
        if r and r[0] == 0:
            live = sorted(int(e) for e in r[2] if e != 0)
            self.log("INFO", f"Active endpoints (live)={live} (stored)={eps}")
            eps = sorted(set(eps) | set(live))

        for ep_id in eps:
            await self._probe_endpoint(ep_id, mfr_code)

        if self.listen_s:
            self._phase = "listen"
            self.log("INFO", f"LISTENING {self.listen_s}s for unsolicited frames — switch each "
                             f"outlet / change the load now; every frame is logged raw")
            await asyncio.sleep(self.listen_s)

        self._phase = "done"
        self.report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        path = await asyncio.to_thread(self._save)
        n_tx = sum(1 for f in self.frames if f.get("dir") == "TX")
        self.log("INFO", f"FULL PROBE done — {len(self.frames) - n_tx} RX / {n_tx} TX frames captured, "
                         f"{len(self.report['errors'])} errors; saved {path}")
        self.report["saved_to"] = path
        return self.report

    async def _probe_endpoint(self, ep_id: int, mfr_code: Optional[int]):
        z = self.zdev
        epr: Dict[str, Any] = {"clusters": {}}
        self.report["endpoints"][ep_id] = epr
        self._phase = f"zdo EP{ep_id}"
        r = await self._req(f"Simple_Desc EP{ep_id}", z.zdo.Simple_Desc_req(z.nwk, ep_id))
        if not (r and r[0] == 0):
            return
        sd = r[2]
        epr.update(profile=f"0x{sd.profile:04X}", device_type=f"0x{sd.device_type:04X}",
                   in_clusters=self._hex(sd.input_clusters),
                   out_clusters=self._hex(sd.output_clusters))
        self.log("INFO", f"══ EP{ep_id} profile=0x{sd.profile:04X} device_type=0x{sd.device_type:04X} "
                         f"in={epr['in_clusters']} out={epr['out_clusters']}")

        ep = z.endpoints.get(ep_id)
        if ep is None:
            self.log("WARNING", f"EP{ep_id} not in stored model — cannot address its clusters")
            return
        for cid in sorted(sd.input_clusters):
            cluster = ep.in_clusters.get(cid)
            if cluster is None:
                cluster = zigpy.zcl.Cluster.from_id(ep, cid, is_server=True)
                self.log("WARNING", f"EP{ep_id} 0x{cid:04X} not in stored model; probing ad hoc")
            await self._probe_cluster(ep_id, cluster, epr, mfr_code)
        for cid in sorted(sd.output_clusters):
            epr["clusters"][f"out 0x{cid:04X}"] = {"note": "client cluster, not read"}

    async def _discover(self, cluster, label: str, manufacturer=None) -> Dict[int, Dict]:
        found: Dict[int, Dict] = {}
        start, extended = 0, True
        for _ in range(16):
            if extended:
                rsp = await self._req(f"{label} discover_ext",
                                      cluster.discover_attributes_extended(start, 16, manufacturer=manufacturer))
                recs = getattr(rsp, "extended_attr_info", None)
                if recs is None and not found:
                    extended = False            # fall back to plain discovery
                    continue
            else:
                rsp = await self._req(f"{label} discover",
                                      cluster.discover_attributes(start, 16, manufacturer=manufacturer))
                recs = getattr(rsp, "attribute_info", None)
            if not recs:
                break
            for rec in recs:
                acl = getattr(rec, "acl", None)
                found[int(rec.attrid)] = {"type": int(rec.datatype),
                                          "acl": None if acl is None else int(acl)}
            if getattr(rsp, "discovery_complete", True):
                break
            start = int(recs[-1].attrid) + 1
        return found

    async def _probe_cluster(self, ep_id: int, cluster, epr: Dict, mfr_code: Optional[int]):
        cid = cluster.cluster_id
        label = f"EP{ep_id} 0x{cid:04X}"
        self._phase = f"probe {label}"
        cr: Dict[str, Any] = {"name": cluster.name, "attributes": {}}
        epr["clusters"][f"in 0x{cid:04X}"] = cr

        attrs = await self._discover(cluster, label)
        m_attrs: Dict[int, Dict] = {}
        if mfr_code:
            m_attrs = await self._discover(cluster, f"{label} mfr", manufacturer=mfr_code)
            m_attrs = {a: v for a, v in m_attrs.items() if a not in attrs}
        self.log("INFO", f"── {label} {cluster.name}: {len(attrs)} attrs {self._hex(attrs)}"
                         + (f", mfr-specific {self._hex(m_attrs)}" if m_attrs else ""))

        for group, mfr in ((attrs, None), (m_attrs, mfr_code)):
            ids = sorted(group)
            for i in range(0, len(ids), READ_CHUNK):
                chunk = ids[i:i + READ_CHUNK]
                n0 = len(self.frames)
                rsp = await self._req(f"{label} read {self._hex(chunk)}",
                                      cluster._read_attributes(chunk, manufacturer=mfr))
                # Pair zigpy's decode with the raw frame the tap just captured
                raw = next((f for f in self.frames[n0:]
                            if f.get("dir") == "RX" and f.get("cluster") == f"0x{cid:04X}"
                            and "read_rsp" in str(f.get("command"))), None)
                raw_by_attr = {r["attr"]: r for r in (raw or {}).get("records", [])}
                for rec in getattr(rsp, "status_records", None) or []:
                    aid = int(rec.attrid)
                    key = f"0x{aid:04X}"
                    known = cluster.attributes.get(aid)
                    meta = group.get(aid, {})
                    acl = meta.get("acl")
                    entry = {
                        "name": known.name if known and not mfr else "?",
                        "discovered_type": _tname(meta.get("type", 0)),
                        "acl": None if acl is None else "".join(
                            c for b, c in ((1, "R"), (2, "W"), (4, "P")) if acl & b),
                        "status": f"0x{int(rec.status):02X}",
                        "zigpy_value": None, "raw": raw_by_attr.get(key),
                        "mfr": f"0x{mfr:04X}" if mfr else None,
                    }
                    if int(rec.status) == 0 and rec.value is not None:
                        entry["zigpy_value"] = repr(rec.value.value)
                    cr["attributes"][key] = entry
                    rv = entry["raw"] or {}
                    self.log("INFO", f"{label} {key} {entry['name']:<28} "
                                     f"type={rv.get('type', entry['discovered_type'])} acl={entry['acl']} "
                                     f"status={entry['status']} value={rv.get('value', entry['zigpy_value'])}"
                                     + (f" (zigpy: {entry['zigpy_value']})"
                                        if rv and str(rv.get('value')) != str(entry['zigpy_value']) else ""))

        # Reporting configuration for every reportable attribute
        rep_ids = [a for a, v in attrs.items() if v.get("acl") is None or v["acl"] & 0x04]
        if rep_ids:
            cr["reporting"] = {}
            for i in range(0, len(rep_ids), 4):
                chunk = rep_ids[i:i + 4]
                recs = [foundation.ReadReportingConfigRecord(direction=0, attrid=a) for a in chunk]
                rsp = await self._req(f"{label} read_reporting {self._hex(chunk)}",
                                      cluster.general_command(
                                          foundation.GeneralCommand.Read_Reporting_Configuration, recs))
                for cfg in getattr(rsp, "attribute_configs", None) or []:
                    c = cfg.config
                    key = f"0x{int(c.attrid):04X}"
                    if cfg.status == foundation.Status.SUCCESS:
                        val = (f"min={c.min_interval}s max={c.max_interval}s "
                               f"change={getattr(c, 'reportable_change', None)}")
                    else:
                        val = f"{cfg.status.name}"
                    cr["reporting"][key] = val
                    self.log("INFO", f"{label} {key} reporting: {val}")

        # Commands the cluster accepts / emits
        for kind, fn in (("received", cluster.discover_commands_received),
                         ("generated", cluster.discover_commands_generated)):
            rsp = await self._req(f"{label} commands_{kind}", fn(0, 32))
            ids = getattr(rsp, "command_ids", None)
            if ids is not None:
                cr[f"commands_{kind}"] = [f"0x{int(c):02X}" for c in ids]
        if cr.get("commands_received") or cr.get("commands_generated"):
            self.log("INFO", f"{label} commands rx={cr.get('commands_received')} "
                             f"tx={cr.get('commands_generated')}")

    def _save(self) -> str:
        os.makedirs(PROBE_DIR, exist_ok=True)
        path = os.path.join(PROBE_DIR, f"{self.ieee.replace(':', '')}_{time.strftime('%Y%m%d_%H%M%S')}.json")
        with open(path, "w") as f:
            json.dump(self.report, f, indent=2, default=str)
        return path
