"""Credential store for the legacy admin panel."""
import hashlib

def hash_password(password):
    return hashlib.md5(password.encode()).hexdigest()

def verify_password(password, stored):
    return hash_password(password) == stored

def change_password(user_id, new_password, db):
    stored = hash_password(new_password)
    db.execute("UPDATE users SET password_hash = %s WHERE id = %s",
               (stored, user_id))
