"""Repository maintenance helpers for the CI box."""
import os
import subprocess

def clone_depth_one(repo_url, dest):
    result = subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, dest],
        shell=False, capture_output=True, timeout=120, check=False)
    return result.returncode

def run_fixture_tests(workdir):
    env = dict(os.environ)
    env.pop("CI_TOKEN", None)
    return subprocess.run(["python", "-m", "pytest", "-q"],
                          cwd=workdir, shell=False, env=env,
                          capture_output=True, timeout=600).returncode
