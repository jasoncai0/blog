#!/usr/bin/env python3
"""把「导出」得到的 notes-<slug>.json 导入同步服务。

用法：
    python3 server/import_notes.py notes-dsh-recall.json                 # 导入到线上
    python3 server/import_notes.py notes-dsh-recall.json --api http://127.0.0.1:8099/api
    python3 server/import_notes.py notes-dsh-recall.json --dry-run       # 只打印，不发请求

文件格式就是站点「导出」按钮产出的那样：{"<slug>": [ {id, q, pre, post, block, t, thread}, ... ]}。
同一个 id 重复导入会覆盖成文件里的版本（服务端 PUT 是幂等的）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_API = "https://jasoncai.site/api"
MAX_PRE_POST = 80
MAX_Q = 400


def trim(value: str | None, limit: int) -> str:
    return (value or "")[:limit]


def normalise(note: dict) -> dict | None:
    """只保留服务端接受的字段，并做长度裁剪。"""
    nid = str(note.get("id") or "").strip()
    q = trim(note.get("q"), MAX_Q).strip()
    if not nid or not q:
        return None
    thread = []
    for item in (note.get("thread") or [])[:20]:
        text = trim(item.get("text"), 4000)
        thread.append(
            {
                "author": trim(item.get("author") or "我", 20) or "我",
                "text": text,
                "t": int(item.get("t") or note.get("t") or 0),
            }
        )
    if not thread:  # 至少要有一条，哪怕是空的（纯高亮）
        thread = [{"author": "我", "text": "", "t": int(note.get("t") or 0)}]
    return {
        "id": nid,
        "q": q,
        "pre": trim(note.get("pre"), MAX_PRE_POST),
        "post": trim(note.get("post"), MAX_PRE_POST),
        "block": int(note.get("block") or 0),
        "t": int(note.get("t") or 0),
        "thread": thread,
    }


def put(api: str, slug: str, note: dict) -> tuple[int, str]:
    url = f"{api}/notes/{slug}/{note['id']}"
    data = json.dumps(note, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method="PUT", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()[:120]
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()[:200]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload = json.loads(open(args.file, encoding="utf-8").read())
    if not isinstance(payload, dict):
        print("文件格式不对：顶层应该是 {slug: [notes]}", file=sys.stderr)
        return 2

    total = ok = skipped = 0
    for slug, notes in payload.items():
        if not isinstance(notes, list):
            print(f"跳过 {slug}：不是数组", file=sys.stderr)
            continue
        for raw in notes:
            total += 1
            note = normalise(raw)
            if not note:
                skipped += 1
                print(f"跳过无效条目 {raw.get('id')!r}")
                continue
            if args.dry_run:
                print(f"[dry] {slug}/{note['id']} q={note['q']!r}")
                ok += 1
                continue
            status, body = put(args.api, slug, note)
            if status in (200, 201):
                ok += 1
            else:
                print(f"失败 {slug}/{note['id']} -> {status} {body}")
        print(f"{slug}: {len(notes)} 条")
    print(f"共 {total} 条，成功 {ok}，跳过 {skipped}")
    return 0 if ok + skipped == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
