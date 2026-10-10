#!/bin/bash
# Kept for anything that still calls it by name; sidecar_service.sh does the work.
exec bash "$(dirname "$0")/sidecar_service.sh" beekeeper
