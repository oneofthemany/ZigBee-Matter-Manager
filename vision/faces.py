"""Faces: find one, align it, measure its pose, turn it into a vector; and the
household's gallery of enrolled people. The models load on first use — most
installs never enrol anyone. See docs/vision.md §Faces."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import assets

logger = logging.getLogger("vision.faces")

SLOTS = ("front", "left", "right", "down")
MODEL_ID = "sface-2021dec"
# OpenCV's published cosine threshold for SFace; a starting point until the
# household's own pictures say otherwise.
MATCH = 0.363
DETECT_SIDE = 640            # faces are found on a copy this size, aligned from the original
MIN_EYE_PX = 24              # closer than this and there's too little face to compare
MAX_CAMERA_SAMPLES = 40
PENDING_S = 600
MAX_PENDING = 8
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")

# Where the five landmarks sit in the 112x112 crop face embedders are trained
# on: eyes, nose tip, mouth corners, image-left first.
TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                     [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)


class FaceError(ValueError):
    """A picture that can't be enrolled; the message is for the person holding the phone."""


# ---- pictures

def decode(data: bytes) -> np.ndarray:
    from PIL import Image, ImageOps
    try:
        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (2 * DETECT_SIDE, 2 * DETECT_SIDE))  # JPEG: decode at 1/2..1/8 for free
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception as e:                                # noqa: BLE001
        raise FaceError("Couldn't read that picture — use a JPEG or PNG") from e
    return np.asarray(im)


def encode_jpeg(rgb: np.ndarray, quality: int = 90) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _sample(img: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Bilinear lookup of img at float coordinates."""
    ih, iw = img.shape[:2]
    x0 = np.clip(np.floor(x).astype(np.intp), 0, iw - 1)
    y0 = np.clip(np.floor(y).astype(np.intp), 0, ih - 1)
    x1, y1 = np.minimum(x0 + 1, iw - 1), np.minimum(y0 + 1, ih - 1)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    f = img.astype(np.float32)
    top = f[y0, x0] * (1 - fx) + f[y0, x1] * fx
    bot = f[y1, x0] * (1 - fx) + f[y1, x1] * fx
    return np.clip(top * (1 - fy) + bot * fy, 0, 255).astype(np.uint8)


def resize(img: np.ndarray, w: int, h: int) -> np.ndarray:
    ih, iw = img.shape[:2]
    ys = np.clip((np.arange(h) + 0.5) * ih / h - 0.5, 0, ih - 1)
    xs = np.clip((np.arange(w) + 0.5) * iw / w - 0.5, 0, iw - 1)
    return _sample(img, *np.meshgrid(xs, ys))


def similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """2x3 least-squares rotation + scale + shift taking src points onto dst (Umeyama)."""
    ms, md = src.mean(0), dst.mean(0)
    s, d = src - ms, dst - md
    u, sig, vt = np.linalg.svd(d.T @ s / len(src))
    e = np.diag([1.0, np.sign(np.linalg.det(u @ vt))])
    rot = u @ e @ vt
    scale = (sig * np.diag(e)).sum() / s.var(0).sum()
    m = np.zeros((2, 3), np.float32)
    m[:, :2] = scale * rot
    m[:, 2] = md - scale * rot @ ms
    return m


def align(rgb: np.ndarray, kp: np.ndarray) -> np.ndarray:
    m = similarity(kp, TEMPLATE)
    inv = np.linalg.inv(np.vstack([m, [0, 0, 1]]))[:2]   # output pixel -> source pixel
    gx, gy = np.meshgrid(np.arange(112, dtype=np.float32), np.arange(112, dtype=np.float32))
    return _sample(rgb, inv[0, 0] * gx + inv[0, 1] * gy + inv[0, 2], inv[1, 0] * gx + inv[1, 1] * gy + inv[1, 2])


def measure(kp: np.ndarray) -> Dict[str, Any]:
    """Yaw: nose across the eye line, in eye widths (+ = toward image right).
    Pitch: nose down the eye-to-mouth span; a head tipped down pushes the nose
    toward the mouth, so it rises."""
    eyes, nose, mouth = kp[:2].mean(0), kp[2], kp[3:].mean(0)
    ew = float(np.linalg.norm(kp[1] - kp[0])) or 1.0
    span = float(mouth[1] - eyes[1]) or 1.0
    yaw, pitch = float(nose[0] - eyes[0]) / ew, float(nose[1] - eyes[1]) / span
    # Left and right as the picture shows them: a mirrored selfie swaps them,
    # and recognition needs both sides, not which is which.
    pose = "down" if pitch >= 0.75 else "left" if yaw <= -0.15 else "right" if yaw >= 0.15 else "front"
    return {"pose": pose, "yaw": round(yaw, 2), "pitch": round(pitch, 2), "eye_px": round(ew, 1)}


def sharpness(face: np.ndarray) -> float:
    """Variance of the Laplacian on the aligned crop: low is blurred."""
    g = face.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    lap = g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:] - 4 * g[1:-1, 1:-1]
    return round(float(lap.var()), 1)


# ---- models

def ort_session(path: Path, threads: int):
    import onnxruntime as ort
    o = ort.SessionOptions()
    o.intra_op_num_threads, o.inter_op_num_threads = threads, 1
    o.log_severity_level = 3
    return ort.InferenceSession(str(path), o, providers=["CPUExecutionProvider"])


class YuNet:
    """Anchor-free: one prediction per grid cell per stride, offsets in cell units."""

    def __init__(self, path: Path, threads: int = 2):
        self.s = ort_session(path, threads)
        shape = self.s.get_inputs()[0].shape
        self.fixed = shape[2] if isinstance(shape[2], int) else None

    def detect(self, rgb: np.ndarray, min_score: float = 0.6) -> List[Tuple[float, Tuple[float, ...], np.ndarray]]:
        h, w = rgb.shape[:2]
        ph, pw = (self.fixed, self.fixed) if self.fixed else (-(-h // 32) * 32, -(-w // 32) * 32)
        pad = np.zeros((ph, pw, 3), np.float32)
        pad[:h, :w] = rgb[:ph, :pw, ::-1]                       # the model reads BGR, 0-255
        out = dict(zip([o.name for o in self.s.get_outputs()],
                       self.s.run(None, {"input": pad.transpose(2, 0, 1)[None]})))
        faces = []
        for st in (8, 16, 32):
            cols = pw // st
            score = np.sqrt(np.clip(out[f"cls_{st}"][0, :, 0], 0, 1) * np.clip(out[f"obj_{st}"][0, :, 0], 0, 1))
            for i in np.nonzero(score >= min_score)[0]:
                r, c = divmod(int(i), cols)
                bx = out[f"bbox_{st}"][0, i]
                cx, cy = (c + bx[0]) * st, (r + bx[1]) * st
                bw, bh = np.exp(bx[2]) * st, np.exp(bx[3]) * st
                kp = (out[f"kps_{st}"][0, i].reshape(5, 2) + [c, r]) * st
                faces.append((float(score[i]), (cx - bw / 2, cy - bh / 2, bw, bh), kp.astype(np.float32)))
        return _nms(faces)


def _nms(faces: list, iou: float = 0.3) -> list:
    keep: list = []
    for f in sorted(faces, key=lambda f: -f[0]):
        if all(_iou(f[1], k[1]) < iou for k in keep):
            keep.append(f)
    return keep


def _iou(a, b) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / (a[2] * a[3] + b[2] * b[3] - inter + 1e-9)


class SFace:
    def __init__(self, path: Path, threads: int = 2):
        self.s = ort_session(path, threads)

    def embed(self, face: np.ndarray) -> np.ndarray:
        v = self.s.run(None, {"data": face.astype(np.float32).transpose(2, 0, 1)[None]})[0][0]   # RGB, 0-255
        return v / (np.linalg.norm(v) + 1e-9)


class FaceModels:
    """YuNet + SFace, fetched and loaded on first use; one picture at a time."""

    def __init__(self, root: Path, threads: int = 2):
        self.root, self.threads = root, threads
        self._lock = threading.Lock()
        self._det: Optional[YuNet] = None
        self._emb: Optional[SFace] = None
        self.note: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self._emb is not None

    def _load(self) -> None:
        if self._emb is None:
            try:
                self._det = YuNet(assets.fetch("yunet.onnx", self.root), self.threads)
                self._emb = SFace(assets.fetch("sface.onnx", self.root), self.threads)
                self.note = None
            except Exception as e:                        # noqa: BLE001
                self.note = f"can't load the face models: {e}"
                raise FaceError(self.note) from e

    def read(self, rgb: np.ndarray) -> List[Dict[str, Any]]:
        """Every face in the picture, largest first: box and pose in the
        picture's own pixels, the aligned crop, and its vector when it's big
        enough to compare (else `error`)."""
        with self._lock:
            self._load()
            h, w = rgb.shape[:2]
            k = min(1.0, DETECT_SIDE / max(h, w))
            small = rgb if k == 1.0 else resize(rgb, max(1, round(w * k)), max(1, round(h * k)))
            out = []
            for score, box, kp in self._det.detect(small):
                kp = kp / k
                crop = align(rgb, kp)
                m = measure(kp)
                face = {"score": round(score, 3), "box": [round(v / k) for v in box], **m,
                        "sharp": sharpness(crop), "crop": crop}
                if m["eye_px"] < MIN_EYE_PX:
                    face["error"] = "Face too small — move closer, or use a sharper picture"
                else:
                    face["vector"] = self._emb.embed(crop)
                out.append(face)
            return sorted(out, key=lambda f: -f["eye_px"])


def subject(faces: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The one face an enrolment photo is of."""
    if not faces:
        raise FaceError("No face found — face the camera, in good light")
    if len(faces) > 1 and faces[1]["eye_px"] >= 0.6 * faces[0]["eye_px"]:
        raise FaceError("More than one face — use a picture of just this person")
    if "error" in faces[0]:
        raise FaceError(faces[0]["error"])
    return faces[0]


def slot_note(slot: str, face: Dict[str, Any]) -> Optional[str]:
    """A warning, not a refusal: the photo is kept either way."""
    if slot == "front" and face["pose"] != "front":
        return "This looks turned or tipped, not straight on"
    if slot in ("left", "right") and face["pose"] == "front":
        return "This looks straight on — turn the head further"
    if slot == "down" and face["pose"] != "down":
        return "The head doesn't look tipped down much"
    return None


# ---- the gallery

def _write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "person"


class Gallery:
    """data/vision/gallery/people.json, and per person <id>/samples.json plus
    <sample>.jpg (the aligned crop). Crops are kept so a new model can
    re-embed rather than everyone re-enrolling; vectors carry their model id."""

    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.RLock()
        self._pending: Dict[str, Tuple[float, Optional[str], List[Dict[str, Any]]]] = {}

    def _people(self) -> List[Dict[str, Any]]:
        try:
            return json.loads((self.root / "people.json").read_text())
        except (OSError, ValueError):
            return []

    def _samples(self, pid: str) -> List[Dict[str, Any]]:
        try:
            return json.loads((self.root / pid / "samples.json").read_text())
        except (OSError, ValueError):
            return []

    def _person(self, pid: str) -> Dict[str, Any]:
        p = next((p for p in self._people() if p["id"] == pid), None) if _ID_RE.match(pid or "") else None
        if p is None:
            raise KeyError(pid)
        return p

    @staticmethod
    def _clean_name(name: Any) -> str:
        name = " ".join(str(name or "").split())
        if not 1 <= len(name) <= 40:
            raise ValueError("A name is 1 to 40 characters")
        return name

    def add_person(self, name: Any) -> Dict[str, Any]:
        name = self._clean_name(name)
        with self._lock:
            people = self._people()
            if any(p["name"].lower() == name.lower() for p in people):
                raise ValueError(f"There's already someone called {name}")
            # Fixed now and never re-derived: signals and rules will key on it.
            base, ids, n = _slug(name), {p["id"] for p in people}, 2
            pid = base
            while pid in ids:
                pid, n = f"{base}_{n}", n + 1
            person = {"id": pid, "name": name, "created": int(time.time())}
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / pid).mkdir(exist_ok=True)
            _write_json(self.root / "people.json", people + [person])
            return person

    def rename(self, pid: str, name: Any) -> Dict[str, Any]:
        name = self._clean_name(name)
        with self._lock:
            self._person(pid)
            people = self._people()
            if any(p["name"].lower() == name.lower() and p["id"] != pid for p in people):
                raise ValueError(f"There's already someone called {name}")
            for p in people:
                if p["id"] == pid:
                    p["name"] = name
            _write_json(self.root / "people.json", people)
            return self._person(pid)

    def delete(self, pid: str) -> None:
        with self._lock:
            self._person(pid)
            _write_json(self.root / "people.json", [p for p in self._people() if p["id"] != pid])
            shutil.rmtree(self.root / pid, ignore_errors=True)

    def _add_sample(self, pid: str, face: Dict[str, Any], **meta) -> Dict[str, Any]:
        sid = secrets.token_hex(4)
        sample = {"id": sid, **meta, **{k: face[k] for k in ("pose", "yaw", "pitch", "eye_px", "sharp")},
                  "added": int(time.time()), "model": MODEL_ID, "vector": [round(float(x), 5) for x in face["vector"]]}
        (self.root / pid / f"{sid}.jpg").write_bytes(encode_jpeg(face["crop"]))
        return sample

    def set_slot(self, pid: str, slot: str, face: Dict[str, Any]) -> Dict[str, Any]:
        """The enrolment photo for one of SLOTS, replacing the one there."""
        if slot not in SLOTS:
            raise ValueError(f"slot is one of {', '.join(SLOTS)}")
        with self._lock:
            self._person(pid)
            samples = self._samples(pid)
            for old in [s for s in samples if s.get("slot") == slot]:
                (self.root / pid / f"{old['id']}.jpg").unlink(missing_ok=True)
            sample = self._add_sample(pid, face, slot=slot, source="upload", note=slot_note(slot, face))
            _write_json(self.root / pid / "samples.json", [s for s in samples if s.get("slot") != slot] + [sample])
            return self._public_sample(sample)

    def hold(self, faces: List[Dict[str, Any]], camera: Optional[str]) -> str:
        """Keep a camera picture's faces for a few minutes while someone picks one."""
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._pending.items() if now - v[0] > PENDING_S]:
                del self._pending[k]
            while len(self._pending) >= MAX_PENDING:
                del self._pending[min(self._pending, key=lambda k: self._pending[k][0])]
            token = secrets.token_hex(8)
            self._pending[token] = (now, camera, faces)
            return token

    def add_from_camera(self, pid: str, token: str, index: int) -> Dict[str, Any]:
        with self._lock:
            self._person(pid)
            held = self._pending.get(str(token))
            if not held or time.time() - held[0] > PENDING_S:
                raise ValueError("That picture has expired — take another")
            faces = held[2]
            if not 0 <= int(index) < len(faces):
                raise ValueError("No such face in that picture")
            face = faces[int(index)]
            if "vector" not in face:
                raise FaceError(face.get("error") or "That face can't be used")
            samples = self._samples(pid)
            if sum(s.get("source") == "camera" for s in samples) >= MAX_CAMERA_SAMPLES:
                raise ValueError(f"{MAX_CAMERA_SAMPLES} camera pictures is the most — remove some first")
            sample = self._add_sample(pid, face, slot=None, source="camera", camera=held[1])
            _write_json(self.root / pid / "samples.json", samples + [sample])
            return self._public_sample(sample)

    def delete_sample(self, pid: str, sid: str) -> None:
        with self._lock:
            self._person(pid)
            samples = self._samples(pid)
            if not any(s["id"] == sid for s in samples):
                raise KeyError(sid)
            (self.root / pid / f"{sid}.jpg").unlink(missing_ok=True)
            _write_json(self.root / pid / "samples.json", [s for s in samples if s["id"] != sid])

    def crop_path(self, pid: str, sid: str) -> Path:
        self._person(pid)
        if not re.fullmatch(r"[0-9a-f]{8}", sid or ""):
            raise KeyError(sid)
        path = self.root / pid / f"{sid}.jpg"
        if not path.is_file():
            raise KeyError(sid)
        return path

    def _vectors(self) -> List[Tuple[str, str, np.ndarray]]:
        """(person, sample, vector) for every sample made with the current model."""
        out = []
        for p in self._people():
            for s in self._samples(p["id"]):
                if s.get("model") == MODEL_ID and s.get("vector"):
                    out.append((p["id"], s["id"], np.asarray(s["vector"], np.float32)))
        return out

    def best_match(self, vector: np.ndarray) -> Optional[Dict[str, Any]]:
        """The enrolled person this face is most like, and how much."""
        with self._lock:
            best: Dict[str, float] = {}
            for pid, _, v in self._vectors():
                best[pid] = max(best.get(pid, -1.0), float(v @ vector))
            if not best:
                return None
            pid = max(best, key=best.get)
            names = {p["id"]: p["name"] for p in self._people()}
            return {"id": pid, "name": names.get(pid, pid), "score": round(best[pid], 3), "match": best[pid] >= MATCH}

    @staticmethod
    def _public_sample(s: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in s.items() if k != "vector"}

    def public(self) -> List[Dict[str, Any]]:
        """People with their samples, and how well the gallery holds together:
        each sample's best agreement with the person's other samples, and the
        nearest other person. Low agreement is a bad photo or the wrong person;
        a near neighbour is someone this could be confused with."""
        with self._lock:
            people = self._people()
            vecs = self._vectors()
            names = {p["id"]: p["name"] for p in people}
            out = []
            for p in people:
                samples = [self._public_sample(s) for s in self._samples(p["id"])]
                mine = [(sid, v) for pid, sid, v in vecs if pid == p["id"]]
                others = [(pid, v) for pid, _, v in vecs if pid != p["id"]]
                for s in samples:
                    v = next((v for sid, v in mine if sid == s["id"]), None)
                    rest = [float(v @ w) for sid, w in mine if sid != s["id"]] if v is not None else []
                    s["agree"] = round(max(rest), 3) if rest else None
                near = None
                for pid, w in others:
                    score = max((float(v @ w) for _, v in mine), default=-1.0)
                    if near is None or score > near["score"]:
                        near = {"id": pid, "name": names.get(pid, pid), "score": round(score, 3)}
                out.append({**p, "samples": samples, "nearest": near if mine else None})
            return out
