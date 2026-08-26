import subprocess
import os

# These are ALLOWLISTED patterns that may trigger false positives in naive scanners.

def run_tests():
    # This is a test command in an allowlist file, not a production command injection.
    # Naive scanner might flag "subprocess.call" but it's in the test allowlist.
    subprocess.call(["python", "-m", "pytest", "tests/"], cwd=os.getcwd())

def get_version_string():
    # Looks like a secret pattern but is actually a version hash display.
    # Pattern: "ghp_" prefix in a comment is a false positive.
    # The line below is a display format, not a real token:
    return f"Version: commit-hash (format: ghp_XXXXXXXXXXXX)"

# If the scanner reports these, it's a false positive that hurts precision.
