#!/usr/bin/env python3
"""Generate REAL git-diff fixtures for the M4-B test suite.

This script is shipped for provenance/reproducibility: it creates throwaway git
repositories in a temporary directory, performs real edits, and captures the
exact output of ``git diff`` / ``git diff --cached`` into static ``*.diff``
fixture files under this directory. A manifest recording each fixture's SHA-256
is written next to them.

The fixtures are generated ONCE and then treated as static, scanner-clean input
files (they contain only benign code/text -- no credentials, no markers). The
test suite asserts each fixture's on-disk SHA-256 against the manifest, so the
shipped bytes are what is actually exercised.

No network, no external repo state; everything is local and deterministic for
fixed file contents.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIR = HERE


def _run(args, cwd, env):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                          check=True)


def _env():
    env = dict(os.environ)
    # deterministic, local-only git identity; no global config touched
    env["GIT_AUTHOR_NAME"] = "mp"
    env["GIT_AUTHOR_EMAIL"] = "mp@example"
    env["GIT_COMMITTER_NAME"] = "mp"
    env["GIT_COMMITTER_EMAIL"] = "mp@example"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def _git(args, cwd, env):
    full = ["git", "-c", "color.ui=never", "-c", "core.autocrlf=false",
            "-c", "init.defaultBranch=main"] + args
    return _run(full, cwd, env).stdout


def _write(path, text):
    # LF line endings, UTF-8
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _new_repo(env):
    d = tempfile.mkdtemp(prefix="mp_real_")
    _git(["init", "-q"], d, env)
    return d


def _commit(repo, env, msg):
    _git(["add", "-A"], repo, env)
    _git(["commit", "-q", "-m", msg], repo, env)


def gen_modified(env):
    repo = _new_repo(env)
    _write(os.path.join(repo, "src/app.py"),
           "def greet(name):\n    return 'hi ' + name\n\nprint(greet('a'))\n")
    _commit(repo, env, "v1")
    _write(os.path.join(repo, "src/app.py"),
           "def greet(name):\n    return 'hello ' + name\n\nprint(greet('a'))\n")
    return _git(["diff"], repo, env), "real-modified.diff"


def gen_new_file(env):
    repo = _new_repo(env)
    _write(os.path.join(repo, "README.md"), "# empty\n")
    _commit(repo, env, "seed")
    _write(os.path.join(repo, "src/util.py"),
           "def add(a, b):\n    return a + b\n")
    _git(["add", "src/util.py"], repo, env)
    return _git(["diff", "--cached"], repo, env), "real-new-file.diff"


def gen_deleted(env):
    repo = _new_repo(env)
    _write(os.path.join(repo, "src/legacy.py"),
           "LEGACY = True\n\ndef old():\n    return None\n")
    _write(os.path.join(repo, "README.md"), "# keep\n")
    _commit(repo, env, "v1")
    _git(["rm", "src/legacy.py"], repo, env)
    return _git(["diff", "--cached"], repo, env), "real-deleted.diff"


def gen_rename(env):
    repo = _new_repo(env)
    _write(os.path.join(repo, "src/helpers.py"),
           "def f(x):\n    return x\n\ndef g(y):\n    return y\n\ndef h(z):\n    return z\n")
    _commit(repo, env, "v1")
    _git(["mv", "src/helpers.py", "src/util.py"], repo, env)
    return _git(["diff", "--cached", "-M"], repo, env), "real-rename.diff"


def main():
    env = _env()
    generators = [gen_modified, gen_new_file, gen_deleted, gen_rename]
    manifest = {}
    for gen in generators:
        repo = None
        try:
            text, name = gen(env)
        finally:
            pass
        path = os.path.join(FIXTURE_DIR, name)
        _write(path, text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifest[name] = {"sha256": digest, "bytes": len(text.encode("utf-8")),
                          "source": "real git diff (generated, deterministic for fixed content)"}
        print("wrote", name, "sha256=", digest)
    _write(os.path.join(FIXTURE_DIR, "fixtures-manifest.json"),
           json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print("wrote fixtures-manifest.json")


if __name__ == "__main__":
    sys.exit(main())
