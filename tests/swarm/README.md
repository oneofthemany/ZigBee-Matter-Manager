# Swarm Intelligence tests

    python3 tests/swarm/run_all.py

Plain scripts, no framework — matching `tests/fuel`. Each module exposes `run()`
returning a `Checker`; `run_all.py` drives them and exits non-zero on failure.

| Module | Covers |
|---|---|
| `test_resolver.py` | One device reduced to offers: capability folding across the three legacy vocabularies, sniffing, contact polarity, boolean coercion, command gating |
| `test_network.py` | The whole swarm: room indexing, coverage counts, and the ranked wiring between devices |
| `test_api.py` | The read-only HTTP surface, driven through Starlette's `TestClient` |

`test_api.py` is skipped where fastapi is not installed; the other two need
nothing beyond the standard library. No test touches the network, a database,
or a real radio — `harness.py` builds fake devices in the four shapes the
resolver has to cope with (Zigbee capabilities object, Matter list accessor,
duck-typed provider, presence user).

## What the fake network is for

`harness.sample_network()` is a small house — a hallway with a radar, a light
and a door contact; a lounge with a plug and a TRV; a Nuki lock; two people. It
exists so the ranking assertions mean something: the tests check that the
pairing everyone would choose by hand is the one that comes out on top, without
that pairing being written down anywhere in the source.
