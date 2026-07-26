#!/bin/bash
# m3b-generate-tokens.sh — 生成 4 个角色 token(reviewer/fixer/verifier/coordinator)。
# 写到 /home/ngh/.config/mergepilot/role-tokens.json(chmod 600),不回显完整值。
# 已存在则不覆盖(除非 --force),避免每次重跑让 worker/controller 失配。
# 用法: wsl -- bash /mnt/d/goai/tools/m3b-generate-tokens.sh [--force]
set -euo pipefail
DIR=/home/ngh/.config/mergepilot
OUT="$DIR/role-tokens.json"
mkdir -p "$DIR"; chmod 700 "$DIR"
FORCE=0; [ "${1:-}" = "--force" ] && FORCE=1

if [ -f "$OUT" ] && [ "$FORCE" = "0" ]; then
  echo "已存在 $OUT(不覆盖;如需重生成加 --force,但必须同步重部署 worker/controller)"
else
  python3 -c "
import secrets, json
toks = {r: secrets.token_urlsafe(32) for r in ('reviewer','fixer','verifier','coordinator')}
print(json.dumps(toks, indent=2))
" > "$OUT"
  chmod 600 "$OUT"
  echo "wrote $OUT (chmod 600)"
fi

# 只回显每角色 token 前 8 位,确认存在 + 互不相同
echo "=== 预览(前 8 位)==="
python3 -c "
import json
d=json.load(open('$OUT'))
for r,t in d.items():
    print(f'  {r:12s}: {t[:8]}... (len={len(t)})')
assert len(set(d.values()))==4, 'token 重复!'
print('  4 token 互不相同 ✓')
"
