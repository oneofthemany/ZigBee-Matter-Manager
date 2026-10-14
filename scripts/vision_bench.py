"""The benchmark behind docs/vision.md, "Cost on the hub": how fast the enrolment models run
on this machine, and how well faces match on the household's own pictures.
Not product code. Needs numpy, onnxruntime, ai-edge-litert and ffmpeg.

    python -I scripts/vision_bench.py speed [--coral] [--threads 1 2]
    python -I scripts/vision_bench.py faces --gallery DIR --probes DIR

`faces`: DIR/<name>/*.jpg; probes of nobody enrolled go in DIR/unknown/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vision.faces import SFace, YuNet, align, measure as pose, resize, sharpness  # noqa: E402

_ZOO = "https://github.com/opencv/opencv_zoo/raw/47534e27c9851bb1128ccc0102f1145e27f23f98/models/"
_CORAL = "https://raw.githubusercontent.com/google-coral/test_data/104342d2d3480b3e66203073dac24f4e2dbb4c41/"

# name -> (url, sha256). YuNet MIT, SFace Apache-2.0, Coral Apache-2.0: docs/vision.md, Models.
MODELS = {
    "yunet.onnx": (_ZOO + "face_detection_yunet/face_detection_yunet_2026may.onnx",
                   "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0"),
    "yunet_640_int8.onnx": (_ZOO + "face_detection_yunet/face_detection_yunet_2023mar_int8.onnx",
                            "321aa5a6afabf7ecc46a3d06bfab2b579dc96eb5c3be7edd365fa04502ad9294"),
    "sface.onnx": (_ZOO + "face_recognition_sface/face_recognition_sface_2021dec.onnx",
                   "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
    "sface_int8.onnx": (_ZOO + "face_recognition_sface/face_recognition_sface_2021dec_int8.onnx",
                        "2b0e941e6f16cc048c20aee0c8e31f569118f65d702914540f7bfdc14048d78a"),
    "effnet_s_embed.tflite": (_CORAL + "efficientnet-edgetpu-S_quant_embedding_extractor.tflite",
                              "d22c826c74288d473f3ea1a8b3d27fa2f3742a06f39027a66bd5a8c3fbd9e6d1"),
    "effnet_s_embed_edgetpu.tflite": (_CORAL + "efficientnet-edgetpu-S_quant_embedding_extractor_edgetpu.tflite",
                                      "56d453080ba30adf9cd593f2eb08d0551fe5ce8de16d05a99da97c4a1715f179"),
    "mnv1_embed.tflite": (_CORAL + "mobilenet_v1_1.0_224_quant_embedding_extractor.tflite",
                          "f409ef4f73fcbb9667841965623c5c8524aac33d41b9bf5f3667161fb4a76319"),
    "mnv1_embed_edgetpu.tflite": (_CORAL + "mobilenet_v1_1.0_224_quant_embedding_extractor_edgetpu.tflite",
                                  "33a249155302975d7e28f72a5250903cdf7f7afb7bd1ceec3d2cd8ca9ce6a609"),
    "face.jpg": (_CORAL + "face.jpg", "270e2bb9a1dce26a042baea93dbc4118d2f9b243c822ae5687ced52715048c56"),
}

def fetch(name: str, root: Path) -> Path:
    url, want = MODELS[name]
    path = root / name
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == want:
        return path
    root.mkdir(parents=True, exist_ok=True)
    data = urllib.request.urlopen(url, timeout=120).read()
    if hashlib.sha256(data).hexdigest() != want:
        sys.exit(f"{name}: checksum mismatch, not using it")
    path.write_bytes(data)
    return path


# ---- images: ffmpeg + numpy, as the sidecar does it (no Pillow in the image)

def probe_size(path: Path) -> tuple:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


def decode(path: Path, vf: str = "", size: tuple = None) -> np.ndarray:
    """RGB array of a JPEG, optionally through an ffmpeg filter that ends at `size`."""
    w, h = size or probe_size(path)
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path)]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3)


def make_still(face: Path, out: Path, w: int, h: int) -> None:
    """A camera-sized JPEG with the test face in it, for decode timings."""
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size={w}x{h}:rate=1", "-i", str(face), "-filter_complex",
                    "[1]scale=-1:" + str(h // 3) + "[f];[0][f]overlay=x=W/3:y=H/4", "-frames:v", "1",
                    "-q:v", "3", str(out)], check=True)


def _draft(Image, path: Path, w: int, h: int) -> np.ndarray:
    im = Image.open(path)
    im.draft("RGB", (w, h))                                     # JPEG decodes at 1/2, 1/4, 1/8 for free
    return np.asarray(im.convert("RGB"))


# ---- generic embedders (objects, zone states)

def tflite(path: Path, threads: int, edgetpu_lib: str = None):
    from ai_edge_litert.interpreter import Interpreter, load_delegate
    if edgetpu_lib:
        it = Interpreter(model_path=str(path), experimental_delegates=[load_delegate(edgetpu_lib)])
    else:
        it = Interpreter(model_path=str(path), num_threads=threads)
    it.allocate_tensors()
    i, o = it.get_input_details()[0], it.get_output_details()[0]

    def run(rgb: np.ndarray) -> np.ndarray:
        it.set_tensor(i["index"], rgb[None])
        it.invoke()
        return it.get_tensor(o["index"]).reshape(-1)
    return run


# ---- timing

def timed(fn, n: int, warm: int = 3) -> dict:
    for _ in range(warm):
        fn()
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    return {"median": round(statistics.median(ts), 1), "p90": round(ts[int(len(ts) * 0.9) - 1], 1)}


def machine() -> dict:
    flags = ""
    try:
        flags = next(l for l in open("/proc/cpuinfo") if l.startswith("flags"))
        model = next(l for l in open("/proc/cpuinfo") if l.startswith("model name")).split(":", 1)[1].strip()
    except (OSError, StopIteration):
        model = platform.processor()
    return {"cpu": model, "arch": platform.machine(), "avx": " avx" in flags, "avx2": " avx2" in flags,
            "cores": os.cpu_count(), "load": os.getloadavg()}


def speed(args) -> None:
    root, n = Path(args.models), args.n
    for name in MODELS:
        if args.coral or "edgetpu" not in name:
            fetch(name, root)
    face_jpg = root / "face.jpg"
    res = {"machine": machine(), "runs": n, "results": []}

    def rec(what, threads, t, **extra):
        row = {"what": what, "threads": threads, **t, **extra}
        res["results"].append(row)
        print(f"  {what:<44} {'' if threads is None else f't={threads}':<4} {t['median']:>8.1f} ms  (p90 {t['p90']:.1f})", flush=True)

    print(json.dumps(res["machine"]))
    tmp = root / "bench"
    tmp.mkdir(exist_ok=True)
    for w, h in ((1920, 1080), (2560, 1440)):
        still = tmp / f"still_{h}.jpg"
        make_still(face_jpg, still, w, h)
        rec(f"decode {w}x{h} jpeg, full", None, timed(lambda: decode(still, size=(w, h)), max(5, n // 4)))
        crop = f"crop={w // 3}:{h // 2}:{w // 3}:0,scale=320:240"
        rec(f"decode {w}x{h} jpeg, crop+scale to 320x240", None,
            timed(lambda: decode(still, crop, (320, 240)), max(5, n // 4)))
        try:                                                    # in-process, if it were added to the image
            from PIL import Image
            rec(f"decode {w}x{h} jpeg, Pillow full", None,
                timed(lambda: np.asarray(Image.open(still).convert("RGB")), n))
            rec(f"decode {w}x{h} jpeg, Pillow draft 1/2", None,
                timed(lambda: _draft(Image, still, w // 2, h // 2), n))
        except ImportError:
            pass

    img = decode(face_jpg)
    for threads in args.threads:
        det = YuNet(root / "yunet.onnx", threads)
        for side in (160, 320, 480, 640):
            x = resize(img, side, side)
            rec(f"yunet {side}x{side}", threads, timed(lambda: det.detect(x), n))
        det8 = YuNet(root / "yunet_640_int8.onnx", threads)
        x640 = resize(img, 640, 640)
        rec("yunet 640x640 int8", threads, timed(lambda: det8.detect(x640), n))

        x = resize(img, 320, 320)
        found = det.detect(x)
        if not found:
            sys.exit("yunet found no face in the test picture — decoding is wrong")
        kp = found[0][2]
        rec("align (numpy) 112x112", threads, timed(lambda: align(x, kp), n))
        face = align(x, kp)
        rec("sharpness + pose", threads, timed(lambda: (sharpness(face), pose(kp)), n))
        for model in ("sface.onnx", "sface_int8.onnx"):
            sf = SFace(root / model, threads)
            rec(f"{model.split('.')[0]} embed", threads, timed(lambda: sf.embed(face), n))
        e32 = SFace(root / "sface.onnx", threads).embed(face)
        e8 = SFace(root / "sface_int8.onnx", threads).embed(face)
        print(f"  (int8 vs float vectors for the same face: cosine {float(e32 @ e8):.3f})")

        sf = SFace(root / "sface.onnx", threads)
        still = tmp / "still_1440.jpg"
        crop = "crop=853:720:853:0,scale=320:270"

        def face_read():
            p = decode(still, crop, (320, 270))
            f = det.detect(p)
            if f:
                a = align(p, f[0][2])
                sharpness(a), pose(f[0][2]), sf.embed(a)
        rec("FACE READ: 1440p still -> vector", threads, timed(face_read, max(5, n // 2)))

        for model in ("effnet_s_embed.tflite", "mnv1_embed.tflite"):
            run = tflite(root / model, threads)
            x224 = resize(img, 224, 224)
            rec(f"{model.split('.')[0]} cpu", threads, timed(lambda: run(x224), n))

    if args.coral:
        from vision import assets
        lib = assets.edgetpu_library(Path(args.coral_assets))
        x224 = resize(img, 224, 224)
        for model in ("effnet_s_embed_edgetpu.tflite", "mnv1_embed_edgetpu.tflite"):
            run = tflite(root / model, 1, str(lib))
            rec(f"{model.split('.')[0]} coral", None, timed(lambda: run(x224), n))

    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=1))
        print(f"wrote {args.out}")


# ---- matching on real pictures

def read_faces(path: Path, det: YuNet, sf: SFace) -> list:
    """Every usable face in one picture: (vector, pose, sharpness, box)."""
    w, h = probe_size(path)
    k = min(1.0, 960 / max(w, h))                               # the size a person crop would be
    sw, sh = max(32, int(w * k) // 2 * 2), max(32, int(h * k) // 2 * 2)
    img = decode(path, f"scale={sw}:{sh}", (sw, sh))
    out = []
    for score, box, kp in det.detect(img):
        a = align(img, kp)
        out.append((sf.embed(a), pose(kp), sharpness(a), [round(v) for v in box]))
    return out


def faces(args) -> None:
    root = Path(args.models)
    det = YuNet(fetch("yunet.onnx", root), args.threads[0])
    sf = SFace(fetch(args.embedder, root), args.threads[0])

    def load(d: Path) -> list:
        rows = []
        for person in sorted(p for p in d.iterdir() if p.is_dir()):
            for img in sorted(person.iterdir()):
                if img.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                    continue
                found = read_faces(img, det, sf)
                if not found:
                    print(f"  no face: {img}")
                    continue
                best = max(found, key=lambda f: f[1]["eye_px"])  # the nearest face is the subject
                rows.append({"who": person.name, "file": str(img), "v": best[0], "pose": best[1], "sharp": best[2]})
        return rows

    gallery, probes = load(Path(args.gallery)), load(Path(args.probes))
    print(f"gallery {len(gallery)} faces, probes {len(probes)} faces")
    if not gallery or not probes:
        sys.exit("nothing to compare")
    g = np.stack([r["v"] for r in gallery])
    names = sorted({r["who"] for r in gallery})
    results = []
    for p in probes:
        sims = g @ p["v"]
        per = {n: float(max(s for s, r in zip(sims, gallery) if r["who"] == n)) for n in names}
        ranked = sorted(per.items(), key=lambda kv: -kv[1])
        best, second = ranked[0], (ranked[1][1] if len(ranked) > 1 else 0.0)
        results.append({"truth": p["who"], "best": best[0], "score": round(best[1], 3),
                        "margin": round(best[1] - second, 3), "pose": p["pose"], "sharp": p["sharp"],
                        "file": p["file"]})
        print(f"  {p['who']:<12} -> {best[0]:<12} {best[1]:.3f} (margin {best[1] - second:+.3f}) "
              f"yaw {p['pose']['yaw']:+.2f} pitch {p['pose']['pitch']:.2f} eyes {p['pose']['eye_px']}px "
              f"sharp {p['sharp']}  {Path(p['file']).name}")

    print("\nthreshold  known-named-right  known-missed  wrongly-named")
    known = [r for r in results if r["truth"] in names]
    for t in np.arange(0.25, 0.65, 0.025):
        right = sum(r["score"] >= t and r["best"] == r["truth"] for r in known)
        wrong = sum(r["score"] >= t and r["best"] != r["truth"] for r in results)
        print(f"  {t:.3f}    {right:>4}/{len(known):<4}          {len(known) - right:>4}        {wrong:>4}")
    if args.out:
        Path(args.out).write_text(json.dumps({"machine": machine(), "results": results}, indent=1))
        print(f"wrote {args.out}")


def coral_check(args) -> None:
    """Why the Edge TPU delegate won't load: the device node first, then the library."""
    import errno
    for node in sorted(Path("/dev").glob("apex_*")) or [Path("/dev/apex_0")]:
        try:
            os.close(os.open(node, os.O_RDWR))
            print(f"{node}: opens read-write")
        except OSError as e:
            print(f"{node}: {errno.errorcode.get(e.errno, e.errno)} {e.strerror}")
    try:
        print("selinux context:", open("/proc/self/attr/current").read().strip("\x00\n"))
    except OSError:
        pass
    from vision import assets
    lib = str(assets.edgetpu_library(Path(args.coral_assets)))
    from ai_edge_litert.interpreter import load_delegate
    try:
        load_delegate(lib)
        print("delegate: loads")
    except ValueError as e:
        print(f"delegate: fails — {str(e).splitlines()[-1] if str(e).strip() else 'no message'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("speed", "faces"):
        p = sub.add_parser(name)
        p.add_argument("--models", default="data/vision/bench-models")
        p.add_argument("--threads", type=int, nargs="+", default=[1, 2])
        p.add_argument("--out")
    sp = sub.choices["speed"]
    sp.add_argument("-n", type=int, default=30)
    sp.add_argument("--coral", action="store_true")
    sp.add_argument("--coral-assets", default="data/vision/models", help="where the sidecar keeps libedgetpu")
    fp = sub.choices["faces"]
    fp.add_argument("--gallery", required=True)
    fp.add_argument("--probes", required=True)
    fp.add_argument("--embedder", default="sface.onnx", choices=["sface.onnx", "sface_int8.onnx"])
    cp = sub.add_parser("coral-check")
    cp.add_argument("--coral-assets", default="data/vision/models")
    args = ap.parse_args()
    {"speed": speed, "faces": faces, "coral-check": coral_check}[args.cmd](args)


if __name__ == "__main__":
    main()
