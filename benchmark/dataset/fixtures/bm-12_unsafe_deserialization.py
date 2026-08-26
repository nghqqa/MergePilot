"""Session cache loader for migrated user profiles."""
import base64
import pickle

def load_cached_profile(cookie_value):
    raw = base64.b64decode(cookie_value)
    profile = pickle.loads(raw)
    return profile.get("preferences", {})

def save_cached_profile(profile):
    raw = pickle.dumps(profile)
    return base64.b64encode(raw).decode()

def get_theme(cookie_value):
    prefs = load_cached_profile(cookie_value)
    return prefs.get("theme", "light")
