#!/usr/bin/env bash
# 一条命令发布：校验 → 构建 → 自检 → 备份 → 上传 → 线上复验 → 失败回滚提示
#
# 用法:
#   scripts/publish.sh [--final] [--dry-run] [--skip-pull] [--skip-live-check] [--target TARGET]
# 环境:
#   BLOG_TARGET   默认 ubuntu@62.234.122.22:/var/www/blog；也可给本地目录（测试用）
#   BLOG_LOCK     默认 /tmp/blog-publish.lock
#   BLOG_SSH_KEY  默认 ~/.dsh/keys/blog_deploy_key
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

TARGET="${BLOG_TARGET:-ubuntu@62.234.122.22:/var/www/blog}"
LOCK="${BLOG_LOCK:-/tmp/blog-publish.lock}"
SITE_URL="${BLOG_SITE_URL:-https://www.jasoncai.site}"
BACKUP_DIR="${BLOG_BACKUP_DIR:-$HOME/backups}"
SSH_KEY="${BLOG_SSH_KEY:-$HOME/.dsh/keys/blog_deploy_key}"
SSH_OPTS=(-i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15
          -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/ssh_known_hosts_js)
FINAL=0
DRY=0
SKIP_PULL=0
SKIP_LIVE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --final) FINAL=1 ;;
    --dry-run) DRY=1 ;;
    --skip-pull) SKIP_PULL=1 ;;
    --skip-live-check) SKIP_LIVE=1 ;;
    --target) TARGET="$2"; shift ;;
    *) echo "未知参数 $1" >&2; exit 2 ;;
  esac
  shift
done

is_remote() { case "$TARGET" in *@*:*) return 0 ;; *) return 1 ;; esac; }
remote_host() { echo "${TARGET%%:*}"; }
remote_path() { echo "${TARGET#*:}"; }

list_target_posts() {
  if is_remote; then
    ssh "${SSH_OPTS[@]}" "$(remote_host)" "ls -1 $(remote_path)/posts 2>/dev/null || true"
  else
    ls -1 "$TARGET/posts" 2>/dev/null || true
  fi | sed 's/\.html$//' | sort
}

backup_target() { # $1=时间戳；输出备份位置
  if is_remote; then
    ssh "${SSH_OPTS[@]}" "$(remote_host)" \
      "mkdir -p ~/backups && cp -a $(remote_path) ~/backups/blog-$1 && echo ~/backups/blog-$1"
  else
    [ -d "$TARGET" ] || { echo ""; return 0; }
    mkdir -p "$BACKUP_DIR"
    cp -a "$TARGET" "$BACKUP_DIR/blog-$1"
    echo "$BACKUP_DIR/blog-$1"
  fi
}

rollback_hint() { # $1=备份位置（可为空）
  local where="$1"
  echo "---- 回滚命令（复制即可执行）----" >&2
  if [ -z "$where" ]; then
    echo "  本次没有产生备份；目标目录可能仍完好，先检查：$(describe_target)" >&2
    return 0
  fi
  if is_remote; then
    echo "  ssh $(remote_host) \"rsync -a --delete $where/ $(remote_path)/\"" >&2
  else
    echo "  rsync -a --delete $where/ $TARGET/" >&2
  fi
}

describe_target() {
  if is_remote; then echo "$(remote_host):$(remote_path)"; else echo "$TARGET"; fi
}

echo "== 1/7 前置检查"
if [ "$SKIP_PULL" -eq 0 ]; then
  git pull --rebase --quiet || { echo "FAIL: git pull 失败"; exit 1; }
fi
if [ -n "$(git status --porcelain)" ] && [ "$DRY" -eq 0 ]; then
  echo "FAIL: 工作区不干净，先提交或 stash" >&2
  exit 1
fi
python3 scripts/check-tags.py content/posts data/tags.yaml || { echo "FAIL: tag 门禁未通过"; exit 1; }

echo "== 2/7 加锁"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "FAIL: 另一个发布正在进行（$LOCK 存在）" >&2
  exit 1
fi
cleanup() { rmdir "$LOCK" 2>/dev/null || true; }
trap cleanup EXIT

echo "== 3/7 构建"
rm -rf public
hugo --minify --cleanDestinationDir >/dev/null

echo "== 4/7 本地自检"
python3 scripts/verify-site.py --public public || { echo "FAIL: 站点自检未通过"; exit 1; }

echo "== 5/7 远端集合比对"
if is_remote; then
  # 连不上就中止：否则会当成「远端没有文章」而跳过并发保护
  ssh "${SSH_OPTS[@]}" "$(remote_host)" true \
    || { echo "FAIL: 连不上 $(remote_host)，未做远端比对，已中止" >&2; exit 1; }
fi
REMOTE_SLUGS=$(list_target_posts) || { echo "FAIL: 读取远端文章列表失败，已中止" >&2; exit 1; }
LOCAL_SLUGS=$(ls -1 public/posts/*.html | xargs -n1 basename | sed 's/\.html$//' | sort)
while IFS= read -r slug; do
  [ -z "$slug" ] && continue
  if ! grep -qx "$slug" <<<"$LOCAL_SLUGS"; then
    echo "FAIL: 远端有本地没有的文章 $slug，先把它合并进来再发布" >&2
    exit 1
  fi
done <<<"$REMOTE_SLUGS"

if [ "$DRY" -eq 1 ]; then
  echo "== DRY-RUN：已完成校验与比对，跳过备份与上传（目标 $(describe_target)）"
  exit 0
fi

echo "== 6/7 备份与上传"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_PATH=$(backup_target "$STAMP") || { echo "FAIL: 备份失败，未上传" >&2; exit 1; }
echo "   备份：${BACKUP_PATH:-（目标尚不存在，跳过）}"

RSYNC_FLAGS=(-a)
[ "$FINAL" -eq 1 ] && RSYNC_FLAGS+=(--delete)
if is_remote; then
  if ! rsync "${RSYNC_FLAGS[@]}" \
      -e "ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/ssh_known_hosts_js" \
      public/ "$(remote_host):$(remote_path)/"; then
    echo "FAIL: 上传失败（可能只传了一部分）" >&2
    rollback_hint "$BACKUP_PATH"
    exit 1
  fi
else
  mkdir -p "$TARGET" 2>/dev/null || true
  if ! rsync "${RSYNC_FLAGS[@]}" public/ "$TARGET/"; then
    echo "FAIL: 上传失败（可能只传了一部分）" >&2
    rollback_hint "$BACKUP_PATH"
    exit 1
  fi
fi

echo "== 7/7 线上复验"
if [ "$SKIP_LIVE" -eq 0 ]; then
  NEWEST=$(ls -1t public/posts/*.html | head -1 | xargs basename)
  for path in "/" "/tags/index.html" "/rss.xml" "/posts/$NEWEST"; do
    # curl 偶尔在写完状态码后收尾失败（代理断流），此时 $? 非零但响应已到；只取前三位。
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$SITE_URL$path" || true)
    code=${code:0:3}
    [ -n "$code" ] || code=000
    if [ "$code" != "200" ]; then
      echo "FAIL: $path 返回 $code" >&2
      rollback_hint "$BACKUP_PATH"
      exit 1
    fi
  done
fi

echo "OK: 已发布 $(echo "$LOCAL_SLUGS" | wc -l | tr -d ' ') 篇；备份 ${BACKUP_PATH:-（无）}"
[ "$FINAL" -eq 1 ] && echo "（本次带 --delete，已清理旧文件）" || true
