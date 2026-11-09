#!/usr/bin/env python3
"""发布前/后的站点完整性校验。

用法:
  python3 scripts/verify-site.py --public public [--baseline FILE] [--base-url URL]
  python3 scripts/verify-site.py --public public --update-baseline   # 内容有意改动后刷新基线
有任何问题即退出码 1；notes（新文章、已删文章）只是提示，不阻断。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blockstats  # noqa: E402

DEFAULT_BASELINE = Path("docs/baseline/local.json")
DEFAULT_VOCAB = Path("data/tags.yaml")
DEFAULT_CONTENT = Path("content/posts")


def vocab_keys(path: Path) -> list[str]:
    keys = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([a-z0-9][a-z0-9-]*):", line)
        if match:
            keys.append(match.group(1))
    return keys


def used_tags(content: Path) -> set[str]:
    """文章里实际写到的 tag —— 只有它们才会有归档页。"""
    tags: set[str] = set()
    for post in content.rglob("*.md"):
        text = post.read_text(encoding="utf-8")
        inline_match = re.search(r"^tags:\s*\[(.*?)\]", text, re.M)
        if inline_match:
            tags.update(re.findall(r"[\"']([^\"']+)[\"']", inline_match.group(1)))
            continue
        block = re.search(r"^tags:\s*\n((?:\s+-\s+.+\n)+)", text, re.M)
        if block:
            tags.update(re.findall(r"-\s+(.+)", block.group(1)))
    return {tag.strip() for tag in tags if tag.strip()}


def update_baseline(public: Path, path: Path) -> dict:
    data = {
        html.stem: blockstats.blockstats(html)
        for html in sorted((public / "posts").glob("*.html"))
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    return data


def verify_site(public: Path, baseline: dict, base_url: str | None = None,
                vocab: Path = DEFAULT_VOCAB, content: Path = DEFAULT_CONTENT,
                notes: list[str] | None = None) -> list[str]:
    errors: list[str] = []
    note = notes.append if notes is not None else (lambda _message: None)
    posts_dir = public / "posts"
    produced = {p.stem for p in posts_dir.glob("*.html")} if posts_dir.is_dir() else set()
    documented = {p.parent.name for p in content.rglob("index.md")}

    # 内容里有的文章必须真被构建出来（比基线更可靠：新文章不需要先登记）
    for slug in sorted(documented - produced):
        errors.append(f"FAIL: 文章没有产出 {slug}.html")

    # 基线里的每一篇都要保持结构不变；集合变化只提示，不阻断
    for slug, expected in sorted(baseline.items()):
        if not isinstance(expected, dict):
            continue
        html = posts_dir / f"{slug}.html"
        if slug not in produced:
            note(f"NOTE: 基线里的 {slug} 已不存在（确认删除后跑 --update-baseline）")
            continue
        actual = blockstats.blockstats(html)
        for key, want in expected.items():
            if actual.get(key) != want:
                errors.append(
                    f"FAIL: {slug} 的 {key} 期望 {want}，实际 {actual.get(key)}"
                    "（有意改动就 --update-baseline）")
    for slug in sorted(produced - set(baseline)):
        note(f"NOTE: 新文章 {slug} 还没进基线（--update-baseline 记录它）")

    if vocab.exists():
        keys = set(vocab_keys(vocab))
        tag_files = {p.stem for p in (public / "tags").glob("*.html")} if (public / "tags").is_dir() else set()
        for tag in sorted(used_tags(content)):
            if tag not in tag_files:
                errors.append(f"FAIL: 缺少 tags 归档页 {tag}.html")
        if "index" not in tag_files:
            errors.append("FAIL: 缺少 tags 索引页 tags/index.html")
        for name in sorted(tag_files - keys - {"index"}):
            errors.append(f"FAIL: tags 多出词表外的归档页 {name}.html")
    else:
        errors.append(f"FAIL: 找不到 tag 词表 {vocab}")

    rss = public / "rss.xml"
    if not rss.exists():
        errors.append("FAIL: 缺少 rss.xml")
    else:
        text = rss.read_text(encoding="utf-8")
        items = len(re.findall(r"<item>", text))
        if items != len(produced):
            errors.append(f"FAIL: rss 条目数 {items} != 文章数 {len(produced)}")
        if items and "<category>" not in text:
            errors.append("FAIL: rss 条目缺少 category")

    sitemap = public / "sitemap.xml"
    if not sitemap.exists():
        errors.append("FAIL: 缺少 sitemap.xml")
    elif base_url:
        urls = re.findall(r"<loc>(.*?)</loc>", sitemap.read_text(encoding="utf-8"))
        for url in urls:
            try:
                request = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(request, timeout=10) as response:
                    if response.status != 200:
                        errors.append(f"FAIL: {url} 返回 {response.status}")
            except Exception as exc:  # noqa: BLE001 - 校验脚本，任何异常都算失败
                errors.append(f"FAIL: {url} 不可达（{exc}）")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public", type=Path, default=Path("public"))
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--vocab", type=Path, default=DEFAULT_VOCAB)
    parser.add_argument("--content", type=Path, default=DEFAULT_CONTENT)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    if args.update_baseline:
        data = update_baseline(args.public, args.baseline)
        print(f"OK: 基线已更新 {args.baseline}（{len(data)} 篇）")
        return

    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline.exists() else {}
    if not baseline:
        print(f"FAIL: 基线为空或不存在 {args.baseline}", file=sys.stderr)
        sys.exit(1)

    notes: list[str] = []
    errors = verify_site(args.public, baseline, args.base_url, args.vocab, args.content, notes)
    for message in notes:
        print(message)
    for error in errors:
        print(error)
    if errors:
        print(f"{len(errors)} 项失败")
        sys.exit(1)
    print(f"OK: {len(baseline)} 篇基线、{len(vocab_keys(args.vocab))} 个 tag、rss/sitemap 齐全")


if __name__ == "__main__":
    main()
