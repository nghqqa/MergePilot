"""Webhook notification relay for workspace integrations."""
import requests

def fetch_webhook_target(workspace_id, db):
    row = db.query("SELECT hook_url FROM integrations WHERE id = %s",
                   (workspace_id,))
    return row["hook_url"] if row else None

def deliver_event(workspace_id, event, db):
    target = fetch_webhook_target(workspace_id, db)
    resp = requests.get(target, params={"event": event}, timeout=5)
    return resp.status_code

def preview_url(user_url):
    r = requests.get(user_url, timeout=5)
    return {"status": r.status_code, "body": r.text[:200]}
