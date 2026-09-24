# -*- coding: utf-8 -*-
"""iso_chain.sandbox — 受限容器执行(被测代码/PoC/测试)。

约束(本轮授权): 非 root、内存/CPU 限制、默认禁网、超时杀灭、
不挂载宿主敏感目录、不注入任何秘密。依赖由预构建的测试镜像提供
(按仓库锁文件版本固定 fastapi/pytest)。
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional

TEST_IMAGE = os.environ.get("ISO_TEST_IMAGE", "mp-iso-test:latest")


def run_in_sandbox(cmd: List[str], *, workspace: str, workdir: str = "/work",
                   timeout_s: int = 120, memory: str = "512m",
                   cpus: float = 1.0) -> Dict[str, Any]:
    """在受限容器内执行 cmd。workspace(宿主,含被测代码)只读挂载到 /work;
    /tmp 用 tmpfs(可写,容量限制)。返回 {ok, exit, stdout, stderr, timed_out}。"""
    docker_args = [
        "docker", "run", "--rm",
        "--network", "none",                 # 默认禁网
        "--user", "1000:1000",               # 非 root
        "--memory", memory, "--cpus", str(cpus),
        "--pids-limit", "128",
        "-v", os.path.abspath(workspace) + ":/work:ro",
        "--tmpfs", "/tmp:rw,size=32m,mode=1777",
        "-w", workdir,
        TEST_IMAGE,
    ] + cmd
    try:
        r = subprocess.run(docker_args, capture_output=True, text=True,
                           timeout=timeout_s)
        return {"ok": r.returncode == 0, "exit": r.returncode,
                "stdout": r.stdout[-8000:], "stderr": r.stderr[-4000:],
                "timed_out": False}
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "exit": None,
                "stdout": (e.stdout or b"").decode("utf-8", "replace")[-4000:],
                "stderr": "timeout after %ss" % timeout_s, "timed_out": True}
    except Exception as e:  # noqa
        return {"ok": False, "exit": None, "stdout": "",
                "stderr": type(e).__name__ + ": " + str(e)[:200],
                "timed_out": False}


def prepare_test_image(fastapi_pin: str = None, pytest_pin: str = None) -> Dict[str, Any]:
    """预构建测试镜像(依赖固定;与被测执行面分离)。幂等:镜像已存在则跳过。"""
    r = subprocess.run(["docker", "image", "inspect", TEST_IMAGE],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return {"built": False, "image": TEST_IMAGE, "note": "already present"}
    pins = ""
    if fastapi_pin:
        pins += "fastapi==%s " % fastapi_pin
    else:
        pins += "fastapi "
    if pytest_pin:
        pins += "pytest==%s" % pytest_pin
    else:
        pins += "pytest"
    dockerfile = (
        "FROM python:3.11-slim\n"
        "RUN useradd -m -u 1000 runner\n"
        "RUN pip install --no-cache-dir %s\n" % pins)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "Dockerfile"), "w", newline="\n") as f:
            f.write(dockerfile)
        b = subprocess.run(["docker", "build", "-t", TEST_IMAGE, "."],
                           cwd=td, capture_output=True, text=True, timeout=600)
    return {"built": b.returncode == 0, "image": TEST_IMAGE,
            "detail": (b.stderr or "")[-300:]}
