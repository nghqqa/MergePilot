#!/bin/bash
# 轻量重置:停 worker → 清 OpenClaw session(保留 config/creds)→ 启。
# 比 clean-reset-workers(删+建+重部署)轻得多,用于每任务前清上下文。
# 用法: bash light-reset-sessions.sh <worker> [worker2 ...]
set -uo pipefail
for W in "$@"; do
  CTR=hiclaw-worker-$W
  echo "=== light-reset $W ==="
  docker start "$CTR" >/dev/null 2>&1   # 确保在运行,才能 exec
  sleep 2
  SDIR=/root/hiclaw-fs/agents/$W/.openclaw/agents/main/sessions
  # 运行时清 session 文件(保留 config/creds/identity)
  docker exec "$CTR" sh -c "rm -f $SDIR/*.jsonl $SDIR/*.jsonl.reset.* 2>/dev/null; echo {} > $SDIR/sessions.json 2>/dev/null; echo \"  cleared: \$(ls $SDIR/*.jsonl 2>/dev/null | wc -l) jsonl left\""
  # kill(SIGKILL,不让 agent graceful shutdown 把内存 session flush 回盘)再 start
  docker kill "$CTR" >/dev/null 2>&1
  docker start "$CTR" >/dev/null 2>&1
  sleep 4
  echo "  $(docker ps --filter name=$CTR --format '{{.Status}}')"
done
echo "done."
