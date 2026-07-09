"""
Application alert API.

Backed by modules/app_alerts.py — the same alerts pushed live over the
WebSocket as `app_alert` events. These endpoints let the frontend render
the alert panel and manage read state.
"""

from fastapi import FastAPI

from modules.app_alerts import get_alert_center


def register_alert_routes(app: FastAPI):

    @app.get("/api/alerts")
    async def list_alerts(include_dismissed: bool = False):
        center = get_alert_center()
        alerts = center.list_alerts(include_dismissed=include_dismissed)
        return {"alerts": alerts, "count": len(alerts)}

    @app.post("/api/alerts/{alert_id}/dismiss")
    async def dismiss_alert(alert_id: str):
        ok = get_alert_center().dismiss(alert_id)
        return {"success": ok} if ok else {"success": False, "error": "Alert not found"}

    @app.post("/api/alerts/clear")
    async def clear_alerts():
        n = get_alert_center().clear_all()
        return {"success": True, "cleared": n}
