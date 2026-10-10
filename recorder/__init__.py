"""
Recorder sidecar: keeps camera footage and cuts clips of events. Runs as its
own container from the app image (``python -m recorder``), owned by the ZMM
Manager, so recording carries on while the app restarts or upgrades. See
docs/recordings.md.

Standalone by design: nothing here imports from modules/. The app imports
recorder.store to read the same folder.
"""

PORT = 8557
