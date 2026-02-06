# **Zigbee Manager: A Python MQTT Gateway for Home Assistant**

## ** Overview**

The **Zigbee Manager** is a high-performance, resilient, and feature-rich application designed to manage a large Zigbee mesh network. It utilizes a modern Python backend (FastAPI, zigpy/bellows) for robust network control and integrates seamlessly with Home Assistant via MQTT Discovery. The application features a real-time single-page web interface for device management, topology visualization, and deep debugging.

## ** Key Features**

### ** Network Management & Device Control**

* **Web Interface (SPA):** A responsive web interface built with modern JavaScript (ESM) and Bootstrap 5\.
* **Device Lifecycle:** Supports device viewing, renaming, removal, and re-interviewing.
* **Remote Control:** Send commands (On/Off, Brightness, Color Temp, Position) to devices, including multi-endpoint routing.
* **Groups Management:** Create native Zigbee groups and control them via the web UI or MQTT.
* **Polling Scheduler:** Manages automatic polling of devices for attribute updates at configurable intervals.
* **Configuration Editor:** Web-based editor for modifying config.yaml and initiating system restarts.

### **🛡️ Stability & Performance**

* **Resilience System:** ZHA-inspired core resilience handling (modules/resilience.py), featuring automatic retry and exponential backoff for transient command failures.
* **NCP Failure Recovery:** Implements a watchdog and recovery logic to automatically handle critical Network Co-Processor (NCP) failures.
* **EZSP Tuning:** Enhanced EZSP configuration logic (modules/config\_enhanced.py) dynamically tunes the coordinator stack settings based on network size for maximum stability.
* **Fast Path Processing:** A specialized, non-blocking pipeline (handlers/fast\_path.py) for time-critical sensor events (e.g., motion/presence) to ensure minimal MQTT publication latency.
* **MQTT Queue:** Uses a non-blocking background queue (modules/mqtt\_queue.py) to prevent event loop stalls during MQTT publishing bursts.

### **🔎 Diagnostics & Debugging**

* **Live Debug Log:** Real-time stream of application logs with filtering capabilities.
* **Packet Capture:** Dedicated Debug Packets modal for capturing and Analysing raw Zigbee Cluster Library (ZCL) frames (handlers/zigbee\_debug.py).
* **Packet Decoding:** Provides deep inspection and human-readable summaries for IAS Zone (0x0500), Occupancy Sensing (0x0406), and Tuya Manufacturer-Specific (0xEF00) packets.
* **Mesh Topology:** Dynamic mesh visualization using D3.js with manual refresh/scan functionality to update Link Quality Indicator (LQI) data.

### **💡 Supported Standards & Quirk Handling**

* **Home Assistant Ready:** Full MQTT Discovery implementation (mqtt.py) for instant integration of devices and groups.
* **Comprehensive Handlers:** Ships with dedicated handlers for all major ZCL clusters, including: Basic (0x0000), Power (0x0001), On/Off (0x0006), Level (0x0008), Thermostat (0x0201), Color (0x0300), multiple Measurement clusters (Temperature, Humidity, Illuminance, etc.), IAS Zone (0x0500), Metering (0x0702), and Electrical Measurement (0x0B04).
* **Vendor Quirks:** Built-in handling for specific device quirks, including [Aqara/Xiaomi (0xFCC0)](https://github.com/oneofthemany/ZigBee-Manager/blob/main/docs/aqara_cluster_guide.md and Tuya (0xEF00) for configuration and feature exposure.

## ** Architecture Overview**

The application is structured into a Python backend core and a thin frontend web interface:

| Component            | Technology                      | Role                                                                                                                                                                                                        |
|:---------------------|:--------------------------------|:------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Core**             | Python (FastAPI, zigpy/bellows) | Manages the Zigbee radio, implements the device lifecycle (joining, removal), and enforces resilience and error handling.                                                                                   |
| **MQTT Service**     | mqtt.py (aiomqtt)               | Handles connection to the MQTT broker, manages automatic reconnection, and implements Home Assistant MQTT Discovery.                                                                                        |
| **Cluster Handlers** | handlers/ package               | The application's intelligence layer. Decodes raw ZCL messages, updates normalized device state, and implements complex device-specific logic (e.g., Aquara TRV configuration, Hue motion sensor handling). |
| **Frontend**         | HTML, Bootstrap 5, D3.js        | A single-page application that connects to the backend via **WebSocket (/ws)** for real-time state updates and event logging.                                                                               |

## **⚙️ Installation**

### **Prerequisites**

You need a Unix-like environment (Linux is recommended) and the following installed:

* Python 3.8+
* pip and venv
* sudo access for service setup
* An MQTT Broker (e.g., Mosquitto) running on your network.

### **Automated Deployment**

The provided [deploy.sh](https://github.com/oneofthemany/ZigBee-Manager/blob/main/deploy.sh) script automates the full setup process, including user creation, virtual environment setup, and systemd service installation:

1. **Ensure Project Files Exist:** Run the script from the directory containing the application files.
2. **Run Deployment:**  
   sudo bash deploy.sh

   This script sets up the necessary environment and services in /opt/zigbee-manager.

## **🛠️ Configuration**

The core configuration file is [config.yaml](https://github.com/oneofthemany/ZigBee-Manager/blob/main/config/config.yaml).

1. **Edit Configuration:** After deployment, edit the file in the install directory:  
   sudo vi /opt/zigbee-manager/config/config.yaml

2. **Review Critical Sections:**
    * **mqtt:** Update broker\_host, username, and password to match your MQTT broker credentials.
    * **zigbee:**
        * port: Must match your USB stick path (e.g., /dev/ttyACM0).
        * channel, pan\_id, network\_key: Used to form or join a Zigbee network.
        * ezsp\_config: Advanced coordinator settings, pre-tuned for large, sensor-heavy networks.
3. **Start Service:**  
   sudo systemctl start zigbee-manager

## **🌐 Web Interface Usage**

Access the web interface at http://YOUR\_IP:8000.

| Tab           | Functionality                                                                    |
|:--------------|:---------------------------------------------------------------------------------|
| **Devices**   | Main device table, showing LQI, status, last seen, and device actions.           |
| **Topology**  | Interactive force-directed graph of the mesh network.                            |
| **Settings**  | Web editor for config.yaml and system restart controls.                          |
| **Debug Log** | Real-time event log and raw packet analyzer (requires debug to be enabled).      |
| **Groups**    | UI for defining and controlling native Zigbee Groups (lights, switches, covers). |

## **🐛 Debugging & Troubleshooting**

The application includes extensive built-in diagnostics. For detailed guides, refer to the documentation files in the docs/ folder.

### **Key Debugging Tools**

1. **Live Logs (/ws):** Real-time log streaming to the browser.
2. **Debug Packets Modal:** Access raw ZCL packet captures to see exactly what data devices are sending.
3. **Log Files:**
    * **logs/zigbee.log**: Main application log.
    * **logs/zigbee\_debug.log**: Detailed packet and handler event logs (only active when debugging is explicitly enabled in the UI or API).

### **Documentation & Guides**

| File                                                                                                                   | Content                                                                                     |
|:-----------------------------------------------------------------------------------------------------------------------|:--------------------------------------------------------------------------------------------|
| [docs/onboarding.md](https://github.com/oneofthemany/ZigBee-Manager/tree/main/docs/onboarding.md)                      | Step-by-step manual for debugging and creating support for new, unsupported devices.        |
| [docs/debugging.md](https://github.com/oneofthemany/ZigBee-Manager/tree/main/docs/debugging.md)                        | Comprehensive guide on using the built-in debugger, filters, and log files.                 |
| [docs/aqara\_cluster\_guide.md](https://github.com/oneofthemany/ZigBee-Manager/tree/main/docs/aqara_cluster_guide.md)  | Detailed explanation of the Aqara manufacturer cluster (0xFCC0) implementation and usage.   |
| [docs/structure.md](https://github.com/oneofthemany/ZigBee-Manager/tree/main/docs/structure.md)                        | File structure map of the application.                                                      |

### **Useful Commands**

| Command                                           | Description                           |
|:--------------------------------------------------|:--------------------------------------|
| sudo systemctl status zigbee-manager              | Check if the main service is running. |
| sudo journalctl \-u zigbee-manager \-f            | View system logs for the service.     |
| sudo tail \-f /opt/zigbee-manager/logs/zigbee.log | View application logs.                |
