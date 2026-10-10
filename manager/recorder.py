"""
Recorder sidecar lifecycle (manager/app_sidecar.py): runs from the app's image
as ``python -m recorder``, in its own container so recording carries on while
the app restarts or upgrades. See docs/recordings.md §Sidecar.

Standalone by design: the manager never imports from modules/.
"""
from manager.app_sidecar import AppSidecar

_sidecar = AppSidecar("recorder", "Recording")

CONTAINER = _sidecar.container
_SVC_TRIGGER, _SVC_STATUS = _sidecar.svc_trigger, _sidecar.svc_status
enabled, service_status, request_service = _sidecar.enabled, _sidecar.service_status, _sidecar.request_service
status, enable, disable, restart, ensure = (_sidecar.status, _sidecar.enable, _sidecar.disable,
                                            _sidecar.restart, _sidecar.ensure)
