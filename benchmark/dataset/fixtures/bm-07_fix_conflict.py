import hashlib

def hash_password(password):
    """Hash password with MD5 (outdated, conflicts with security fix)."""
    # PR conflicts with an existing security fix that requires bcrypt.
    # The existing fix (already merged to main) changed this to:
    #   import bcrypt; return bcrypt.hashpw(password.encode(), bcrypt.gensalt())
    # This PR reverts to MD5, creating a merge conflict.
    return hashlib.md5(password.encode()).hexdigest()

def verify_password(password, hashed):
    """Verify password against MD5 hash."""
    return hashlib.md5(password.encode()).hexdigest() == hashed
