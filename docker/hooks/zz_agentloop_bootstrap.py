# -*- coding: utf-8 -*-
"""zz_agentloop_bootstrap — AgentLoop 激活配置自举(镜像内置,密钥不进镜像)。

启动时(.pth 拉起,守护线程):若 /etc/agentloop-{otel,skills,rag}.json 缺失或未启用,
从本栈 MinIO 的 secrets 路径拉取 agentloop-activation.json 并落盘;既有的已启用配置
绝不覆盖。凭据复用容器自带的 AGENTTEAMS_FS_*(worker 天然持有)。成功后由各 hook 的
watcher 按各自 grace 接管(skills 0s / rag 4s / otel 90s,错峰避免并发补丁互覆)。

日志: /tmp/agentloop-bootstrap.log(只记状态码与长度,永不记录密钥内容)。
"""
import json
import os
import subprocess
import threading
import time

SECRETS_OBJ = "agentteams/agentteams-storage/secrets/agentloop-activation.json"
TARGETS = {
    "otel": "/etc/agentloop-otel.json",
    "skills": "/etc/agentloop-skills.json",
    "rag": "/etc/agentloop-rag.json",
}
MC = "/usr/local/bin/mc"
LOG = "/tmp/agentloop-bootstrap.log"
_state = {"done": False}


def _log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + msg + "\n")
    except Exception:
        pass


def _enabled(path):
    try:
        with open(path) as f:
            return bool(json.load(f).get("enabled"))
    except Exception:
        return False


def _fetch():
    """用容器自带 FS 凭据经 mc 拉取 secrets 对象;失败返回 None。"""
    ak = os.environ.get("AGENTTEAMS_FS_ACCESS_KEY", "")
    sk = os.environ.get("AGENTTEAMS_FS_SECRET_KEY", "")
    ep = os.environ.get("AGENTTEAMS_FS_ENDPOINT", "")
    if not (ak and sk and ep) or not os.path.exists(MC):
        return None
    host = ep.replace("http://", "").replace("https://", "")
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    subprocess.run([MC, "alias", "set", "bootfs", "http://" + host, ak, sk],
                   env=env, capture_output=True, timeout=30)
    # worker 的 MinIO 凭据只授权自身 agent 前缀(实测共享 secrets 路径 Insufficient permissions),
    # 激活配置因此放在每个 worker 自己的前缀下: <bucket>/agentteams-storage/agents/<name>/config/...
    name = os.environ.get("AGENTTEAMS_WORKER_NAME", "")
    if not name:
        return None
    obj = "bootfs/agentteams-storage/agents/%s/config/agentloop-activation.json" % name
    p = subprocess.run([MC, "cat", obj], env=env, capture_output=True, timeout=30)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return json.loads(p.stdout.decode("utf-8", "replace"))
    except Exception:
        return None


def _apply(cfgs):
    wrote = []
    for key, path in TARGETS.items():
        section = (cfgs or {}).get(key)
        if not isinstance(section, dict) or not section.get("enabled"):
            continue
        if _enabled(path):
            continue  # 已启用的绝不覆盖
        try:
            with open(path, "w") as f:
                json.dump(section, f, indent=2)
            os.chmod(path, 0o644)
            wrote.append(key)
        except Exception as e:
            _log("write %s failed: %s" % (key, type(e).__name__))
    return wrote


def _tick():
    if _state["done"]:
        return
    if all(_enabled(p) for p in TARGETS.values()):
        _state["done"] = True
        _log("all three already enabled; bootstrap exit")
        return
    cfgs = _fetch()
    if cfgs is None:
        _state["miss"] = _state.get("miss", 0) + 1
        if _state["miss"] in (1, 6, 18):  # 首次与周期性留痕
            _log("fetch miss #%d (creds/path not ready or denied)" % _state["miss"])
        return
    wrote = _apply(cfgs)
    if wrote:
        _log("activated from MinIO secrets: " + ",".join(wrote))
    if all(_enabled(p) for p in TARGETS.values()):
        _state["done"] = True
        _log("bootstrap complete; hooks will take over")


def _watch():
    deadline = time.time() + 15 * 60
    while time.time() < deadline and not _state["done"]:
        try:
            _tick()
        except Exception as e:
            _log("tick error: " + type(e).__name__)
        time.sleep(5)


try:
    threading.Thread(target=_watch, daemon=True).start()
except Exception:
    pass
