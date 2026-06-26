"""
Media player subsystem

A self-contained multi-room audio engine for the ZigBee/Matter manager.
Controls Google Cast (Nest/Home) and WiiM/LinkPlay players, plays internet
radio (Radio-Browser directory), and broadcasts to *native* speaker groups
(Cast groups created in Google Home; WiiM multiroom).

Design: thin, stateless provider objects behind clean ABCs, orchestrated by a
provider-agnostic controller. No MQTT/HA dependency, no ffmpeg stream server —
radio URLs are handed directly to the devices, which fetch them natively.

This borrows the two-sided provider split (sources vs players) from Music
Assistant (Apache-2.0) as a reference, but the abstractions and code are our
own so we can fix the bugs we don't like.
"""

from modules.media.service import MediaService

__all__ = ["MediaService"]
