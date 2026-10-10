"""The model and the Edge TPU runtime, fetched once into the data dir and
checked against pinned hashes. Neither is in the app image: most installs
never enable detection. See docs/vision.md §Models."""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("vision.assets")

_CORAL = "https://raw.githubusercontent.com/google-coral/test_data/104342d2d3480b3e66203073dac24f4e2dbb4c41/"
_EDGETPU = "https://github.com/feranick/libedgetpu/releases/download/16.0TF2.19.1-1/"

# SSDLite MobileDet, COCO, 320x320, Apache-2.0 (Google). name -> (url, sha256)
FILES = {
    "mobiledet_cpu.tflite": (_CORAL + "ssdlite_mobiledet_coco_qat_postprocess.tflite",
                             "32c486140391eb4dc43fca7113ad392be632dc5366687f2731f73d740678693f"),
    "mobiledet_edgetpu.tflite": (_CORAL + "ssdlite_mobiledet_coco_qat_postprocess_edgetpu.tflite",
                                 "b69e508ef2a670e06b80bd3e5559a827d5cd8d557c95d5e332cbf1d31d434a2e"),
    "coco_labels.txt": (_CORAL + "coco_labels.txt",
                        "dc183f003fc753c4c43fae6fdf7f387559449573f13fa32e517fb7453fd380f1"),
    # Built against the TFLite 2.19 that ai-edge-litert 1.2 ships; the two must move together.
    "libedgetpu_amd64.deb": (_EDGETPU + "libedgetpu1-std_16.0tf2.19.1-1.bookworm_amd64.deb",
                             "23be53c72eff4d44afc2f727700da185791d3ca0867bd0b5e082ec3a0de21925"),
    "libedgetpu_arm64.deb": (_EDGETPU + "libedgetpu1-std_16.0tf2.19.1-1.bookworm_arm64.deb",
                             "46ab47310d6de120bda4678febe5eb6143f5c7d7dd17c0d017ae5f6cd67dfec5"),
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(name: str, root: Path, opener=urllib.request.urlopen) -> Path:
    """Path to a verified copy of `name`, downloading it if need be."""
    url, want = FILES[name]
    path = root / name
    if path.is_file() and _sha256(path) == want:
        return path
    root.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    logger.info("downloading %s", name)
    with opener(url, timeout=120) as r, open(tmp, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 16), b""):
            f.write(chunk)
    got = _sha256(tmp)
    if got != want:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{name} failed its checksum — not using it")
    os.replace(tmp, path)
    return path


def edgetpu_library(root: Path) -> Optional[Path]:
    """libedgetpu.so.1 unpacked from its pinned package, or None on a platform
    there is no build for."""
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if not arch:
        return None
    deb = fetch(f"libedgetpu_{arch}.deb", root)
    out = root / f"edgetpu_{arch}"
    found = sorted(out.rglob("libedgetpu.so.1.0")) if out.is_dir() else []
    if not found or out.stat().st_mtime < deb.stat().st_mtime:
        subprocess.run(["dpkg-deb", "-x", str(deb), str(out)], check=True, capture_output=True)
        found = sorted(out.rglob("libedgetpu.so.1.0"))
    return found[0] if found else None
