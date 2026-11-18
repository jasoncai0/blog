#!/usr/bin/env bash
# 把 DSH 的 API key 写进 bridge 的 600 环境文件，然后重启 launchd 任务。
# 用法：bash scripts/set-bridge-key.sh            （交互输入）
#       bash scripts/set-bridge-key.sh sk-xxx     （直接给）
set -euo pipefail
ENV_FILE="$HOME/.dsh/notes-bridge.env"
AGENT="gui/$(id -u)/site.jasoncai.notes-bridge"

key="${1:-}"
if [ -z "$key" ]; then
  read -r -s -p "粘贴 DEEPSEEK_API_KEY（输入不回显）: " key
  echo
fi
[ -n "$key" ] || { echo "没给 key" >&2; exit 1; }

umask 077
touch "$ENV_FILE"
# 去掉旧的 key 行，再追加新行
grep -v '^DEEPSEEK_API_KEY=' "$ENV_FILE" > "$ENV_FILE.tmp" || true
printf 'DEEPSEEK_API_KEY=%s\n' "$key" >> "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"

launchctl kickstart -k "$AGENT" 2>/dev/null || true
echo "已写入 $ENV_FILE 并重启 bridge；看日志： tail -f ~/Library/Logs/notes-bridge.log"
