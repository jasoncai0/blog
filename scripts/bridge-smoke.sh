#!/usr/bin/env bash
# 本地冒烟：注册会话 → 发一条问句批注 → 等 bridge 处理 → 断言出现 AI 楼层
# 用法：bash scripts/bridge-smoke.sh http://127.0.0.1:8099 [seconds]
set -euo pipefail

API="${1:-http://127.0.0.1:8099}"
WAIT="${2:-20}"
SLUG="smoke-$(date +%s)"
NOTE="smk-${SLUG##*-}"
KEY="${NOTES_API_TOKEN:-}"

h=(-H "Content-Type: application/json")
[ -n "$KEY" ] && h+=(-H "Authorization: Bearer $KEY")

curl -sf "${h[@]}" -X PUT "$API/api/sessions/$SLUG" \
  -d '{"session_id":"0f3a1c2d-1234-4abc-9def-001122334455","workspace":"default-workspace"}' >/dev/null

curl -sf "${h[@]}" -X PUT "$API/api/notes/$SLUG/$NOTE" -d "{
  \"id\":\"$NOTE\",\"q\":\"收敛是必要条件\",\"pre\":\"\",\"post\":\"\",\"block\":4,
  \"t\":$(date +%s000),
  \"thread\":[{\"author\":\"我\",\"text\":\"这里为什么不用向量时钟？\",\"t\":$(date +%s000)}]
}" >/dev/null

curl -sf "${h[@]}" -X POST "$API/api/notes/$SLUG/$NOTE/ask" -d '{}' >/dev/null
echo "queued: $SLUG/$NOTE"

for _ in $(seq 1 "$WAIT"); do
  n=$(curl -sf "$API/api/notes/$SLUG" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len(d["notes"][0]["thread"]))')
  if [ "$n" -ge 2 ]; then
    echo "OK: AI floor $n"
    curl -sf "${h[@]}" -X DELETE "$API/api/notes/$SLUG/$NOTE" >/dev/null
    exit 0
  fi
  sleep 1
done
echo "TIMEOUT: no AI floor after ${WAIT}s" >&2
exit 1
