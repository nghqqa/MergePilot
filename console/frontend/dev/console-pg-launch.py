# console-pg-launch.py — console_pg 只读服务的开发启动器（测试基础设施，dev/test only）。
#
# 后端 server.py 的 orchestrator_v3 为"跨域加载由调用方负责"——本启动器按
# tests/console_pg/test_server_http.py 的同款方式注册包别名后调用其 main()。
# 仅绑定 127.0.0.1；DSN 指向隔离 fixture 库；不作为生产服务。
#
# 用法：python dev/console-pg-launch.py [--port 4193] [--dsn "<pg dsn>"]
import argparse
import importlib.util
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 主仓库（含 tools/）在 D:\goai\MergePilot；worktree 在 D:\goai\mp-worktrees\*。
# 从 dev 目录向上找 MergePilot/tools/orchestrator。
_ORCH = None
_CON = None
for _p in _HERE.parents:
    cand = _p / "MergePilot" / "tools"
    if (cand / "orchestrator" / "pg_runstore.py").is_file():
        _ORCH = cand / "orchestrator"
        _CON = cand / "console_pg"
        break
if _ORCH is None:
    print("ERROR: 未找到 MergePilot/tools/orchestrator（主仓库路径）", flush=True)
    sys.exit(2)

_oppkg = types.ModuleType("orchestrator_v3")
_oppkg.__path__ = [str(_ORCH)]
sys.modules["orchestrator_v3"] = _oppkg


def _load(full, path):
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


srv = _load("console_pg_server", _CON / "server.py")

# server.py 的审批路径依赖两个外部约定（与 tests/console_pg 同款）：
#   1) sys.modules['approval_pkg'] → tools/approval（--approval-dsn 时 import）
#   2) sys.path 含 tools/（from approval.approval import Binding）
_TOOLS = _CON.parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))
_apppkg = types.ModuleType("approval_pkg")
_apppkg.__path__ = [str(_TOOLS / "approval")]
sys.modules["approval_pkg"] = _apppkg

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=4193)
ap.add_argument("--dsn", default=(
    "host=127.0.0.1 port=55432 user=mp_contract "
    "password=mp-contract-local-test dbname=mp_pg_runstore"))
ap.add_argument("--approval-dsn", default=None,
                help="隔离审批库 DSN（提供后审批只读端点可用）")
ap.add_argument("--allow-test-auth", action="store_true",
                help="启用隔离联调测试主体（X-Test-Principal）；生产模式严禁")
args = ap.parse_args()
cli = ["--dsn", args.dsn, "--port", str(args.port)]
if args.approval_dsn:
    cli += ["--approval-dsn", args.approval_dsn]
if args.allow_test_auth:
    cli += ["--allow-test-auth"]
sys.exit(srv.main(cli))
