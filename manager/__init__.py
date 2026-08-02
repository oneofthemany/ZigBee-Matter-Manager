"""
ZMM manager sidecar — an always-on status and recovery service on :8001.

A second member of the 'zmm' pod, sharing the host network namespace so it
reaches the app on 127.0.0.1:8000, and mounting the container-runtime socket so
it can inspect and manage the pod's containers. Intentionally small and
self-contained: it stays up while the app restarts. See docs/upgrades.md.
"""
