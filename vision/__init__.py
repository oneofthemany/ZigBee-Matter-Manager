"""
Object-detection sidecar: watches camera streams and reports person, vehicle
and animal presence. Runs as its own container from the app image
(``python -m vision``), owned by the ZMM Manager. See docs/vision.md.

Standalone by design: nothing here imports from modules/.
"""

# What a camera can be asked to report, and the COCO labels behind each.
GROUPS = {
    "person": ("person",),
    "vehicle": ("car", "truck", "bus", "motorcycle", "bicycle"),
    "animal": ("cat", "dog", "bird", "horse", "sheep", "cow", "bear"),
}
LABEL_GROUP = {label: group for group, labels in GROUPS.items() for label in labels}
PORT = 8556
