#!/bin/bash
# e2e-lib.sh — MergePilot L2 E2E 测试共享入口(B4d/B4e 及以后;B4c 脚本已回用)。
#
# 职责:
#   1. 统一目标仓库:E2E_OWNER / E2E_REPO / E2E_BASE_BRANCH(默认指向 **fixture** 仓库
#      nghqqa/MergePilot-e2e-fixture,绝不默认生产 nghqqa/MergePilot)。
#   2. **生产保护门 e2e_guard**:目标为生产仓库 nghqqa/MergePilot 时直接拒绝
#      (除非显式 export ALLOW_PRODUCTION_E2E=1)。**必须在任何 GitHub 写操作之前调用**。
#   3. 测试 Gateway 容器名(E2E_GW,默认 policy-gw-e2e)+ 独立测试角色令牌路径。
#
# 用法(每个 E2E 脚本顶部):
#   source "$(dirname "$0")/e2e-lib.sh"
#   e2e_guard                       # 写操作前必调
#   e2e_GW fixer --call create_branch owner="$E2E_OWNER" repo="$E2E_REPO" ...
#
# 设计:脚本层 guard + Gateway 层 fixture-only policy = 纵深防御(任一层都能挡住生产污染)。

# ── 目标仓库(默认 fixture;勿改默认指向生产)──
: "${E2E_OWNER:=nghqqa}"
: "${E2E_REPO:=MergePilot-e2e-fixture}"
: "${E2E_BASE_BRANCH:=main}"

# ── 测试 Gateway 容器(独立;生产为 policy-gw)──
: "${E2E_GW:=policy-gw-e2e}"

# ── 测试角色令牌(独立文件;由 run-policy-gateway-e2e.sh 生成)──
_E2E_CFG=/home/ngh/.config/mergepilot
: "${E2E_TOKENS_FILE:=$_E2E_CFG/role-tokens-e2e.json}"

# 生产仓库全名(保护门判定用)
E2E_PRODUCTION_REPO="nghqqa/MergePilot"

e2e_repo(){ echo "$E2E_OWNER/$E2E_REPO"; }

# 经测试 Gateway 调 MCP 工具(替代旧的 `docker exec policy-gw python3 /tmp/probe-tools.py`)
e2e_GW(){ docker exec "$E2E_GW" python3 /tmp/probe-tools.py "$@"; }

# 生产保护门:目标 = 生产仓库 → 拒(除非 ALLOW_PRODUCTION_E2E=1)。写操作前必调。
e2e_guard(){
  if [ "$(e2e_repo)" = "$E2E_PRODUCTION_REPO" ]; then
    if [ "${ALLOW_PRODUCTION_E2E:-0}" != "1" ]; then
      cat >&2 <<EOF
REFUSED(e2e_guard): E2E 目标为生产仓库 $E2E_PRODUCTION_REPO。
  生产仓曾被 B4c 测试污染(已清理,见 PR #143)。L2 E2E 必须跑在 fixture 仓库
  (nghqqa/MergePilot-e2e-fixture)。如确需生产 E2E,显式 export ALLOW_PRODUCTION_E2E=1
  (留审计痕迹)。已中止,**未发生任何 GitHub 写操作**。
EOF
      exit 2
    fi
    echo "WARN(e2e_guard): ALLOW_PRODUCTION_E2E=1 — E2E 直跑生产仓 $E2E_PRODUCTION_REPO(已留痕)。" >&2
  fi
}

# 读测试 coordinator token(供 controller/GW 鉴权;缺失返回空)
e2e_coordinator_token(){
  python3 -c "import json;print(json.load(open('$E2E_TOKENS_FILE')).get('coordinator',''))" 2>/dev/null || echo ""
}
