"""
Swarm Intelligence — command-line triage.

    python3 -m modules.swarm.doctor                    # full report
    python3 -m modules.swarm.doctor --explain <id>     # one pattern, per room
    python3 -m modules.swarm.doctor --suggestions      # what would be offered
    python3 -m modules.swarm.doctor --json             # machine-readable

Runs against the state on disk — the device cache, the saved rules, config.yaml
and the pattern directories — so it works when the app is down, which is when a
report is most wanted. It reads nothing the app has open for writing.

The device cache holds state but not live capability objects, so a device's
capabilities are resolved by attribute sniffing alone here. Actuator offers
depend on a live command list and are therefore under-reported: use the
/api/swarm/diagnostics endpoint for a definitive read on a running system.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

DATA_DIR = os.environ.get("ZMM_DATA_DIR", "./data")
STATE_CACHE = os.path.join(DATA_DIR, "device_state_cache.json")
SETTINGS = os.path.join(DATA_DIR, "device_settings.json")
NAMES = os.path.join(DATA_DIR, "names.json")
RULES = os.path.join(DATA_DIR, "automations.json")

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, YELLOW, GREEN, CYAN = "\033[31m", "\033[33m", "\033[32m", "\033[36m"

LEVEL_COLOUR = {"error": RED, "warning": YELLOW, "info": CYAN}


class _CachedDevice:
    """A device reconstructed from the state cache.

    Enough for the resolver: state to sniff, a name, and an empty command list.
    """

    def __init__(self, ieee: str, name: str, state: Dict[str, Any],
                 model: str = "Unknown") -> None:
        self.ieee = ieee
        self.friendly_name = name
        self.state = state
        self.model = model
        self.manufacturer = "Unknown"

    def get_control_commands(self) -> List[Dict[str, Any]]:
        return []


def _load(path: str, default: Any) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_offline() -> Dict[str, Any]:
    """Devices, names, settings and rules as they sit on disk."""
    cache = _load(STATE_CACHE, {}) or {}
    names = _load(NAMES, {}) or {}
    settings = _load(SETTINGS, {}) or {}
    rules_raw = _load(RULES, {}) or {}
    rules = rules_raw.get("rules", rules_raw) if isinstance(rules_raw, dict) else rules_raw

    devices = {}
    for ieee, entry in cache.items():
        state = entry.get("state", entry) if isinstance(entry, dict) else {}
        if not isinstance(state, dict):
            continue
        devices[ieee] = _CachedDevice(
            ieee, names.get(ieee, ieee), state,
            (entry or {}).get("model", "Unknown") if isinstance(entry, dict) else "Unknown")

    return {"devices": devices, "names": names, "settings": settings,
            "rules": rules if isinstance(rules, list) else []}


def _print_report(report: Dict[str, Any]) -> None:
    status = f"{GREEN}OK{RESET}" if report["ok"] else f"{RED}PROBLEMS{RESET}"
    counts = report["counts"]
    print(f"\n{BOLD}Swarm Intelligence — diagnostics{RESET}   {status}"
          f"   ({counts['error']} error, {counts['warning']} warning, "
          f"{counts['info']} info)  {DIM}{report['took_ms']}ms{RESET}\n")

    for f in report["findings"]:
        colour = LEVEL_COLOUR.get(f["level"], "")
        print(f"  {colour}{f['level'].upper():<7}{RESET} {BOLD}{f['code']}{RESET}")
        print(f"          {f['message']}")
        if f.get("fix"):
            print(f"          {DIM}fix: {f['fix']}{RESET}")
        for key in ("devices", "rules", "patterns", "details", "rooms",
                    "capabilities"):
            items = f.get(key)
            if not items:
                continue
            shown = items[:6]
            for item in shown:
                text = item if isinstance(item, str) else \
                    item.get("name") or item.get("title") or item.get("id") or str(item)
                extra = ""
                if isinstance(item, dict) and item.get("blocked_slots"):
                    extra = f"  {DIM}blocked at: {', '.join(item['blocked_slots'])}{RESET}"
                print(f"            - {text}{extra}")
            if len(items) > len(shown):
                print(f"            {DIM}... and {len(items) - len(shown)} more{RESET}")
        print()


def _print_explain(result: Dict[str, Any]) -> None:
    pattern = result["pattern"]
    print(f"\n{BOLD}{pattern['id']}{RESET} — {pattern['title']}")
    print(f"  scope: {pattern.get('scope', 'room')}   "
          f"outcome: {result['outcome']}   candidates: {result['candidates']}\n")
    for t in result["trace"]:
        where = t.get("room_label") or t.get("room") or "house"
        mark = f"{GREEN}match{RESET}" if t["outcome"] == "matched" else f"{YELLOW}no match{RESET}"
        print(f"  {BOLD}{where}{RESET}  {mark}")
        if t.get("reason"):
            print(f"    {DIM}{t['reason']}{RESET}")
        for slot, info in (t.get("slots") or {}).items():
            if info["status"] == "filled":
                note = f"  ({info['note']})" if info.get("note") else ""
                alts = f"  {DIM}+{info['alternatives']} alternative(s){RESET}" \
                    if info.get("alternatives") else ""
                print(f"    {GREEN}✓{RESET} {slot:<8} {info['device']} "
                      f"{DIM}[{info['offer']}]{RESET}{note}{alts}")
            else:
                tag = "optional" if info.get("optional") else "REQUIRED"
                print(f"    {RED}✗{RESET} {slot:<8} unfilled — {info['reason']} "
                      f"{DIM}({tag}){RESET}")
        print()


def _print_suggestions(built: Dict[str, Any]) -> None:
    s = built["summary"]
    c = built["coverage"]
    print(f"\n{BOLD}Suggestions{RESET}  {s['available']} available, "
          f"{s['active']} already built, {s['patterns_unmatched']} pattern(s) "
          f"matched nothing")
    print(f"{BOLD}Coverage{RESET}     {c['percent']}% of devices take part in a "
          f"rule ({c['uncovered']} gap(s))\n")
    for item in built["suggestions"]:
        mark = f"{DIM}[built]{RESET}" if item["status"] != "available" else "       "
        room = item.get("room_label") or item.get("room") or "house"
        print(f"  {mark} {CYAN}{room:<10}{RESET} {item['sentence']}")
    if built["rejected"]:
        print(f"\n  {RED}{len(built['rejected'])} candidate(s) withheld:{RESET}")
        for r in built["rejected"][:10]:
            print(f"    - {r['pattern']} ({r['stage']}): {r['error']}")
    print()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="swarm.doctor", description="Swarm Intelligence triage")
    parser.add_argument("--explain", metavar="PATTERN_ID",
                        help="why one pattern matched, or did not, per room")
    parser.add_argument("--suggestions", action="store_true",
                        help="list what would be suggested")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if repo not in sys.path:
        sys.path.insert(0, repo)

    from modules.swarm import diagnostics as dx
    from modules.swarm import suggestions as sg
    from modules.swarm.network import describe_network, load_rooms

    loaded = load_offline()
    if not loaded["devices"]:
        print(f"{YELLOW}No devices in {STATE_CACHE} — nothing to diagnose.{RESET}\n"
              f"Run from the repository root, or set ZMM_DATA_DIR.")
        return 2

    rooms = load_rooms()
    described = describe_network(loaded["devices"], loaded["names"],
                                 loaded["settings"], rooms)["devices"]

    if args.explain:
        result = dx.explain(args.explain, described, rooms)
        if "error" in result:
            print(f"{RED}{result['error']}{RESET}")
            print("known patterns: " + ", ".join(result.get("known", [])))
            return 2
        print(json.dumps(result, indent=2, default=str) if args.json
              else "", end="")
        if not args.json:
            _print_explain(result)
        return 0

    built = sg.build(described, rules=loaded["rules"], rooms=rooms,
                     names=loaded["names"])

    if args.suggestions:
        if args.json:
            print(json.dumps(built, indent=2, default=str))
        else:
            _print_suggestions(built)
        return 0

    # The cache holds state but not command lists, so actuation cannot be
    # detected here; the report is told so it does not misreport that as a fault.
    report = dx.diagnose(described, built=built, rules=loaded["rules"],
                         rooms=rooms, commands_available=False)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
