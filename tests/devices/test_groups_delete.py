"""
Deleting a group keeps it deleted. Needs zigpy (the lockfile venv, see AGENTS.md);
reported as skipped without it.

Groups live in three places: ZMM's registry (groups.json), zigpy's group table
(persisted in the coordinator database) and each member device's own table.
Deleting only touched the registry and the devices, so the startup resync
re-adopted the group from zigpy — every deleted group came back after a restart.
The LightLink handler's coordinator-only groups were adopted the same way.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from harness import Checker

COORD = "00:00:00:00:00:00:00:01"


def run() -> Checker:
    c = Checker("groups_delete")
    try:
        import zigpy.endpoint
        import zigpy.group
        import zigpy.types as t
    except ImportError:
        print("  SKIPPED: needs zigpy (see AGENTS.md, The dev box)")
        return c
    import modules.groups as groups_mod

    class FakeEP(zigpy.endpoint.Endpoint):
        """A real zigpy Endpoint (zigpy checks isinstance) whose radio call is scripted."""
        def __init__(self, ieee, eid=1, offline=False, log=None):
            super().__init__(SimpleNamespace(ieee=t.EUI64.convert(ieee)), eid)
            self._member_of = {}
            self.offline, self.log = offline, log

        @property
        def device(self):
            return self._device

        async def remove_from_group(self, grp_id):
            self.log.append(str(self.device.ieee))
            if self.offline:
                await asyncio.sleep(30)                      # never answers; the delete must not wait for it
            if grp_id in self.member_of:
                self.member_of[grp_id].remove_member(self)
            return 0

    def build(tmp, registry, zigpy_groups):
        groups_mod.GROUPS_FILE = Path(tmp) / "groups.json"
        Path(groups_mod.GROUPS_FILE).write_text(json.dumps(registry))
        app = SimpleNamespace(state=SimpleNamespace(node_info=SimpleNamespace(ieee=t.EUI64.convert(COORD))))
        app.groups = zigpy.group.Groups(app)
        for gid, eps in zigpy_groups.items():
            g = app.groups.add_group(gid, f"zigpy {gid}")
            for ep in eps:
                g.add_member(ep)
        service = SimpleNamespace(app=app, devices={}, mqtt=None, friendly_names={})
        return groups_mod.GroupManager(service), app

    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        live = FakeEP("aa:aa:aa:aa:aa:aa:aa:01", log=calls)
        gone = FakeEP("aa:aa:aa:aa:aa:aa:aa:02", offline=True, log=calls)    # unplugged since
        coord = FakeEP(COORD, log=calls)
        registry = {"groups": {"5": {"id": 5, "name": "Kitchen", "type": "light", "capabilities": [],
                                     "members": ["aa:aa:aa:aa:aa:aa:aa:01"]}}, "next_id": 6}
        mgr, app = build(tmp, registry, {5: [live, gone, coord]})

        c.section("deleting a group")
        res = asyncio.run(asyncio.wait_for(mgr.remove_group(5), 20))
        c.check("it succeeds even with a member that never answers", res.get("success"), res)
        c.check("every member zigpy knew of is asked to leave — including one the registry had lost",
                "aa:aa:aa:aa:aa:aa:aa:02" in calls and "aa:aa:aa:aa:aa:aa:aa:01" in calls, calls)
        c.check("the coordinator isn't sent a radio command", COORD not in calls, calls)
        c.check("the group is gone from zigpy's table (what the resync re-adopted from)", 5 not in app.groups)
        c.check("…and from the registry", 5 not in mgr.groups)
        saved = json.loads(Path(groups_mod.GROUPS_FILE).read_text())
        c.check("the deletion is remembered on disk", saved.get("deleted_ids") == [5], saved)

        c.section("after a restart")
        app.groups.add_group(5, "came back").add_member(FakeEP("aa:aa:aa:aa:aa:aa:aa:02", log=[]))  # e.g. re-learnt
        mgr2, _ = build(tmp, saved, {})
        mgr2.service.app = app
        mgr2.resync_from_zigbee()
        c.check("the resync doesn't bring a deleted group back, even if zigpy has it again", 5 not in mgr2.groups, mgr2.groups)
        c.check("new groups never reuse a deleted id",
                mgr2.next_group_id > 5 and 5 in getattr(mgr2, "deleted_ids", set()), mgr2.next_group_id)

    with tempfile.TemporaryDirectory() as tmp:
        c.section("what the resync adopts")
        bulb = FakeEP("bb:bb:bb:bb:bb:bb:bb:01", log=[])
        mgr, app = build(tmp, {"groups": {}, "next_id": 1},
                         {0x0000: [FakeEP(COORD, log=[])],                 # LightLink default group
                          0x4001: [FakeEP(COORD, log=[])],                 # a remote's touchlink group
                          9: [bulb, FakeEP(COORD, 2, log=[])]})            # a real group the registry lost
        added = mgr.resync_from_zigbee()
        c.check("coordinator-only groups (LightLink's) aren't adopted", 0 not in mgr.groups and 0x4001 not in mgr.groups, list(mgr.groups))
        c.check("a real group the registry lost still is, without the coordinator as a member",
                added == 1 and mgr.groups.get(9, {}).get("members") == ["bb:bb:bb:bb:bb:bb:bb:01"], mgr.groups.get(9))
        saved = json.loads(Path(groups_mod.GROUPS_FILE).read_text())
        c.check("groups.json is written atomically (no temp file left)", not Path(groups_mod.GROUPS_FILE).with_suffix(".tmp").exists())
        c.check("…and the next id stays above every group", saved["next_id"] > 9, saved["next_id"])
    return c
