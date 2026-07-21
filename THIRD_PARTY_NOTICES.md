# Third-Party Notices

ZigBee Matter Manager is licensed under the GNU General Public License v3.0
(see [LICENSE](LICENSE)). It uses the third-party components listed below.
Each component remains under its own license; nothing in this file modifies
those licenses.

## Vendored frontend libraries

These are redistributed in this repository under `static/js/vendor/` with
their original license headers intact.

| Component | Copyright | License |
|---|---|---|
| [Apache ECharts](https://echarts.apache.org/) (`echarts.min.js`) | The Apache Software Foundation | Apache-2.0 |
| [pdf.js](https://mozilla.github.io/pdf.js/) (`pdf.min.js`, `pdf.worker.min.js`) | Mozilla Foundation | Apache-2.0 |

Both are provided under the Apache License, Version 2.0:
<http://www.apache.org/licenses/LICENSE-2.0>. They are distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
or implied.

## Python dependencies

Installed from PyPI at build/deploy time (see `requirements.txt`); not
redistributed in this repository.

| Package | License |
|---|---|
| aiohttp | Apache-2.0 |
| aiomqtt | BSD-3-Clause |
| bellows | GPL-3.0 |
| cryptography | Apache-2.0 / BSD-3-Clause (dual) |
| duckdb | MIT |
| fastapi | MIT |
| greeclimate | MIT |
| httpx | BSD-3-Clause |
| kokoro-onnx | MIT |
| markdown-it-py | MIT |
| midea-local | MIT |
| numpy | BSD-3-Clause |
| pychromecast | MIT |
| pydantic | MIT |
| python-matter-server | Apache-2.0 |
| python-multipart | Apache-2.0 |
| pyyaml | MIT |
| sounddevice | MIT |
| tidalapi | LGPL-3.0 |
| tzdata | Apache-2.0 |
| uvicorn | BSD-3-Clause |
| zeroconf | LGPL-2.1 |
| zha-quirks (zhaquirks) | Apache-2.0 |
| zigpy | GPL-3.0 |
| zigpy-znp | GPL-3.0 |

## Acknowledgements

- **[zigpy](https://github.com/zigpy/zigpy) / [bellows](https://github.com/zigpy/bellows)** — the Zigbee stack this project is built on.
- **[python-matter-server](https://github.com/home-assistant-libs/python-matter-server)** — the Home Assistant team's Matter/CHIP SDK wrapper, run as a managed subprocess for Matter support.
- **[ZHA](https://www.home-assistant.io/integrations/zha/), [zhaquirks](https://github.com/zigpy/zha-device-handlers), and [Zigbee2MQTT](https://www.zigbee2mqtt.io/)** — prior art whose published documentation and design patterns (device quirks, group handling, coordinator resilience) informed independent implementations in this project. No source code from these projects is included.
- **[Home Assistant MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)** — the published integration protocol this project implements for HA interoperability.
- **[OpenStreetMap](https://www.openstreetmap.org/)** — map tiles are proxied/cached from tile.openstreetmap.org. Map data © OpenStreetMap contributors, available under the [Open Database License](https://www.openstreetmap.org/copyright).
- **[Open-Meteo](https://open-meteo.com/)** — free weather API used by the Heating Advisor.

"Home Assistant" is a trademark of Nabu Casa, Inc. This project is not
affiliated with or endorsed by Nabu Casa, the Zigbee2MQTT project, or the
Music Assistant project.
