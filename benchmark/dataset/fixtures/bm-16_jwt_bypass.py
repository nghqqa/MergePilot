"""Token gate for the reporting API."""
import jwt

SECRET = "changeme"

def decode_token(token):
    return jwt.decode(token, SECRET, algorithms=["none", "HS256"])

def current_user(request):
    token = request.headers.get("X-Auth", "")
    if not token:
        return None
    try:
        return decode_token(token)
    except jwt.InvalidTokenError:
        return None

def view_report(request, db):
    user = current_user(request)
    if user is None:
        return {"error": "unauthenticated"}, 401
    return db.query("SELECT * FROM reports WHERE tenant = %s",
                    (user["tenant"],))
