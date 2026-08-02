"""
Media player subsystem — a self-contained multi-room audio engine for Cast and
WiiM/LinkPlay, internet radio, and native speaker groups.

Thin stateless providers behind ABCs, orchestrated by a provider-agnostic
controller. No MQTT/HA dependency and no stream server on the ordinary path:
radio URLs go straight to the devices. See docs/speaker_sync.md.
"""

from modules.media.service import MediaService

__all__ = ["MediaService"]
