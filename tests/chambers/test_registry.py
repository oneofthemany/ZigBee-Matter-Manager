"""The registry is one list of rooms: heating's and the plan's rooms are its
chambers, under their own ids and the registry's names."""

from __future__ import annotations

import copy

from harness import Checker, square

from modules import chambers as ch

TRV = "tt:00:00:00:00:00:00:0a"
SENSOR = "aa:00:00:00:00:00:00:01"


def hub_config() -> dict:
    """The shape a live hub had: chambers made by name beside heating's rooms."""
    return {
        "chambers": [{"id": "kitchen", "name": "Kitchen", "icon": "fa-utensils"},
                     {"id": "master_bedroom", "name": "Master Bedroom"},
                     {"id": "elena_s_room", "name": "Elena's Room"}],
        "heating": {"circuits": [{"id": "c1", "rooms": [
            {"id": "room_k", "name": "Kitchen", "trvs": [{"ieee": TRV.upper()}],
             "temperature_sensors": [{"ieee": SENSOR}]},
            {"id": "room_m", "name": "Master Bedrom"},
            {"id": "room_e", "name": "Elena"}]}]},
    }


def hub_plan() -> dict:
    return {"levels": [{"id": "ground", "name": "Ground", "index": 0, "rooms": [
        {"id": "room_k", "name": "Kitchen", "polygon": square(0, 0)},
        {"id": "room_l", "name": "Landing", "polygon": square(5, 0)}]}]}


def run() -> Checker:
    c = Checker("registry")

    c.section("before reconciling, the registry already lists every room once per id")
    cfg, plan = hub_config(), hub_plan()
    reg = {r["id"]: r for r in ch.build_registry(cfg, plan)}
    c.check("a room both heated and drawn is one chamber with both facets",
            reg["room_k"]["heated"] and reg["room_k"]["mapped"], reg["room_k"])
    c.check("its level is the plan's", reg["room_k"].get("level") == "ground", reg["room_k"])
    c.check("a drawn-only room is a chamber too",
            reg["room_l"]["mapped"] and not reg["room_l"]["heated"], reg["room_l"])
    c.check("an unused chamber is not in use", not reg["kitchen"]["in_use"], reg["kitchen"])

    c.section("duplicates are offered for merging")
    dups = {frozenset((d["a"], d["b"])): d for d in ch.duplicate_candidates(list(reg.values()))}
    c.check("same name is an exact duplicate",
            dups.get(frozenset(("room_k", "kitchen")), {}).get("exact") is True, dups)
    c.check("a misspelling is offered", frozenset(("room_m", "master_bedroom")) in dups, dups)
    c.check("a possessive and 'room' are offered", frozenset(("room_e", "elena_s_room")) in dups, dups)
    c.check("two rooms both in use are never offered",
            not any(reg[d["a"]]["in_use"] and reg[d["b"]]["in_use"] for d in dups.values()))
    numbered = [{"id": "bed_1", "name": "Bedroom 1", "in_use": True},
                {"id": "bed_2", "name": "Bedroom 2", "in_use": False}]
    c.check("a differing number is a different room", ch.duplicate_candidates(numbered) == [])

    c.section("reconciling registers heating and plan rooms")
    result = ch.reconcile(cfg, plan)
    ids = [x["id"] for x in cfg["chambers"]]
    c.check("every heating and plan room is registered",
            {"room_k", "room_m", "room_e", "room_l"} <= set(ids), ids)
    c.check("an unused chamber of the same name is folded in, not doubled",
            "kitchen" not in ids and result["merged"] == {"kitchen": "room_k"}, result)
    kitchen = next(x for x in cfg["chambers"] if x["id"] == "room_k")
    c.check("and keeps the icon it was given", kitchen.get("icon") == "fa-utensils", kitchen)
    c.check("a near-name is left for the user to confirm", "master_bedroom" in ids, ids)
    again = copy.deepcopy(cfg)
    c.check("reconciling twice changes nothing",
            ch.reconcile(again, copy.deepcopy(plan)) == {"plan_changed": False,
                                                         "heating_changed": False, "merged": {}}
            and again == cfg)

    c.section("one name everywhere")
    ch.upsert_chamber(cfg, {"id": "room_k", "name": "Kitchen Diner"})
    result = ch.reconcile(cfg, plan)
    c.check("a registry rename reaches heating",
            cfg["heating"]["circuits"][0]["rooms"][0]["name"] == "Kitchen Diner")
    c.check("and the plan", plan["levels"][0]["rooms"][0]["name"] == "Kitchen Diner")
    c.check("and says both changed", result["plan_changed"] and result["heating_changed"], result)

    prev = ch.heating_room_names(cfg)
    cfg["heating"]["circuits"][0]["rooms"][2]["name"] = "Elena's Room"
    ch.reconcile(cfg, plan, edited="heating", prev_names=prev)
    c.check("a rename made in heating becomes the chamber's name",
            ch.get_chamber(cfg, "room_e")["name"] == "Elena's Room", ch.get_chamber(cfg, "room_e"))

    prev = ch.plan_room_names(plan)
    plan["levels"][0]["rooms"][1]["name"] = "Upstairs Landing"
    ch.reconcile(cfg, plan, edited="floor_plan", prev_names=prev)
    c.check("a rename made on the plan becomes the chamber's name",
            ch.get_chamber(cfg, "room_l")["name"] == "Upstairs Landing")

    prev = ch.plan_room_names(plan)
    stale = copy.deepcopy(plan)
    stale["levels"][0]["rooms"][0]["name"] = "Kitchen"
    ch.reconcile(cfg, stale, edited="floor_plan", prev_names={"room_k": "Kitchen"})
    c.check("an unchanged stale name on the saved side does not undo a rename",
            ch.get_chamber(cfg, "room_k")["name"] == "Kitchen Diner"
            and stale["levels"][0]["rooms"][0]["name"] == "Kitchen Diner")

    c.section("creating and deleting")
    _, err = ch.upsert_chamber(cfg, {"name": "kitchen diner"})
    c.check("a second chamber of an existing name is refused", err and "already exists" in err, err)
    study, err = ch.upsert_chamber(cfg, {"name": "Study"})
    c.check("a new name is created", study and study["id"] == "study", (study, err))
    cfg["chambers"].append({"id": "office", "name": "Office"})
    office2, _ = ch.upsert_chamber(cfg, {"name": "Office!"})
    c.check("a slug that lands on another chamber's id gets its own",
            office2 and office2["id"] == "office_2", office2)
    ok, err = ch.delete_chamber(cfg, "room_k", plan)
    c.check("a chamber heating uses cannot be deleted", not ok and "heating" in err, err)
    ok, _ = ch.delete_chamber(cfg, "study", plan)
    c.check("an unused one can", ok and ch.get_chamber(cfg, "study") is None)

    c.section("merging")
    out, err = ch.merge_chambers(cfg, plan, "room_m", "master_bedroom")
    c.check("the id heating uses survives", out and out["survivor"] == "room_m", (out, err))
    c.check("under the name merged into",
            cfg["heating"]["circuits"][0]["rooms"][1]["name"] == "Master Bedroom")
    c.check("and the other is gone", ch.get_chamber(cfg, "master_bedroom") is None)
    _, err = ch.merge_chambers(cfg, plan, "room_k", "room_m")
    c.check("two rooms heating or the plan both use cannot merge", err and "both" in err, err)

    c.section("heating decides where its devices are")
    bound = ch.heating_device_rooms(cfg)
    c.check("a TRV belongs to its heating room, normalised", bound.get(TRV) == "room_k", bound)
    c.check("so does a room sensor", bound.get(SENSOR) == "room_k", bound)
    return c


if __name__ == "__main__":
    raise SystemExit(1 if run().failures else 0)
