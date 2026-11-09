#!/usr/bin/env bash
# publish.sh 的行为测试：全部在临时目录里跑，不碰真实服务器。
set -uo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
PASS=0
FAIL=0

report() { # $1=want $2=got $3=name
  if [ "$1" = "$2" ]; then
    echo "ok   - $3"; PASS=$((PASS + 1))
  else
    echo "FAIL - $3 (want $1, got $2)"; FAIL=$((FAIL + 1))
  fi
}

make_site() { # $1 = 目录；复制一份可构建的最小站点
  cp -R "$REPO/hugo.toml" "$REPO/layouts" "$REPO/data" "$REPO/assets" "$REPO/scripts" "$1/"
  mkdir -p "$1/content/posts"
  git -C "$1" init -q
  git -C "$1" add -A >/dev/null 2>&1
  git -C "$1" -c user.name=t -c user.email=t@t commit -qm init >/dev/null 2>&1
}

add_post() { # $1=站点 $2=slug $3=tags 行
  mkdir -p "$1/content/posts/$2"
  printf -- '---\ntitle: "%s"\ndate: 2026-11-09\nsummary: "s"\n%s\nslug: "%s"\n---\n\n正文。\n' \
    "$2" "$3" "$2" > "$1/content/posts/$2/index.md"
  git -C "$1" add -A >/dev/null 2>&1
  git -C "$1" -c user.name=t -c user.email=t@t commit -qm "add $2" >/dev/null 2>&1
}

# --- case 1: 缺 tag 必须失败，且目标目录不被创建 ---
D1=$(mktemp -d); T1=$(mktemp -d)/remote
make_site "$D1"; add_post "$D1" "bad" ""
out=$(cd "$D1" && BLOG_TARGET="$T1" BLOG_BACKUP_DIR="$D1/bk" ./scripts/publish.sh --skip-pull --skip-live-check 2>&1); rc=$?
report 1 "$rc" "缺 tag 时发布失败"
[ -e "$T1" ] && report absent present "失败时目标目录未被创建" || report absent absent "失败时目标目录未被创建"

# --- case 2: 正常发布 ---
D2=$(mktemp -d); T2=$(mktemp -d)/remote
make_site "$D2"; add_post "$D2" "good" 'tags: ["meta"]'
out=$(cd "$D2" && BLOG_TARGET="$T2" BLOG_BACKUP_DIR="$D2/bk" ./scripts/publish.sh --skip-pull --skip-live-check 2>&1); rc=$?
report 0 "$rc" "正常发布成功"
[ -f "$T2/posts/good.html" ] && report present present "目标目录有文章" || report present missing "目标目录有文章"
[ -f "$T2/tags/index.html" ] && report present present "目标目录有 tag 索引" || report present missing "目标目录有 tag 索引"

# --- case 3: 锁存在时立即失败 ---
D3=$(mktemp -d); T3=$(mktemp -d)/remote; L3=$(mktemp -d)/lock
make_site "$D3"; add_post "$D3" "good2" 'tags: ["meta"]'
mkdir "$L3"
out=$(cd "$D3" && BLOG_TARGET="$T3" BLOG_LOCK="$L3" BLOG_BACKUP_DIR="$D3/bk" ./scripts/publish.sh --skip-pull --skip-live-check 2>&1); rc=$?
report 1 "$rc" "锁被占用时发布失败"

# --- case 4: 远端多出文章必须中止 ---
D4=$(mktemp -d); T4=$(mktemp -d)/remote; mkdir -p "$T4/posts"; touch "$T4/posts/ghost.html"
make_site "$D4"; add_post "$D4" "good3" 'tags: ["meta"]'
out=$(cd "$D4" && BLOG_TARGET="$T4" BLOG_BACKUP_DIR="$D4/bk" ./scripts/publish.sh --skip-pull --skip-live-check 2>&1); rc=$?
report 1 "$rc" "远端多出文章时中止"
echo "$out" | grep -q "ghost" && report yes yes "错误信息点名 ghost" || report yes no "错误信息点名 ghost"

echo "----"
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
