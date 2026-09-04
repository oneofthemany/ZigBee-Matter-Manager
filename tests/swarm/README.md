# Swarm Intelligence tests

    python3 tests/swarm/run_all.py

Plain scripts, no framework — matching `tests/fuel`. Each module exposes `run()`
returning a `Checker`; `run_all.py` drives them and exits non-zero on failure.

| Module | Covers |
|---|---|
| `test_resolver.py` | One device reduced to offers: capability folding across the three legacy vocabularies, sniffing, contact polarity, boolean coercion, command gating |
| `test_network.py` | The whole swarm: room indexing, coverage counts, and the ranked wiring between devices |
| `test_virtual.py` | The house-scope inputs that are not devices: the computed flags, and that a missing reading is dropped rather than published as zero |
| `test_stigmergy.py` | Pattern schema and validation, plus a check that every shipped pattern references a real offer |
| `test_suggestions.py` | Matching, condition-vs-prerequisite placement, parameter overrides, what varies, and wiring-based deduplication |
| `test_offers.py` | The `offer` step: validation, the accept/decline lifecycle, expiry, the double-tap, and the cap |
| `test_diagnostics.py` | Every finding the triage report can raise, and the per-pattern explain trace |
| `test_api.py` | The HTTP surface, driven through Starlette's `TestClient`, including the applying path |
| `js/test_sentence.js` | The shared humanizer — contact polarity, outlets, zones, commands, sequences, and a whole rule as one block |
| `js/test_swarm_suggest.js` | The chooser's browser code, sliced out of the shipped `.js`: pairing-to-rule conversion, escaping, and the source-device guard |

`test_api.py` is skipped where fastapi is not installed; the others need nothing
beyond the standard library. `test_api.py` covers the one mutating route, so it
is worth installing fastapi into a scratch venv rather than leaving it skipped:

    python3 -m venv /tmp/venv && /tmp/venv/bin/pip install fastapi httpx
    /tmp/venv/bin/python tests/swarm/run_all.py No test touches the network, a database,
or a real radio — `harness.py` builds fake devices in the four shapes the
resolver has to cope with (Zigbee capabilities object, Matter list accessor,
duck-typed provider, presence user).

## What the fake network is for

`harness.sample_network()` is a small house — a hallway with a radar, a light
and a door contact; a lounge with a plug and a TRV; a Nuki lock; two people. It
exists so the ranking and matching assertions mean something: the tests check
that the pairing everyone would choose by hand is the one that comes out on top,
and that the hallway radar produces the hallway rule, without either being
written down anywhere in the source.
