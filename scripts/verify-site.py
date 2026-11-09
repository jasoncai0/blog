#!/usr/bin/env python3
"""发布前/后的站点完整性校验。

用法:
  python3 scripts/verify-site.py --public public [--baseline docs/baseline/local.json] [--base-url URL]
有任何问题即退出码 1。
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


def vocab_keys(path: Path) -> list[str]:
    keys = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([a-z0-9][a-z0-9-]*):\s*$", line)
        if match:
            keys.append(match.group(1))
    return keys


def verify_site(public: Path, baseline: dict, base_url: str | None = None,
                vocab: Path = DEFAULT_VOCAB) -> list[str]:
    errors: list[str] = []
    posts_dir = public / "posts"
    produced = {p.stem for p in posts_dir.glob("*.html")} if posts_dir.is_dir() else set()

    for slug in sorted(set(baseline) - produced):
        errors.append(f"FAIL: 文章缺失 {slug}.html")
    for slug in sorted(produced - set(baseline)):
        errors.append(f"FAIL: 文章集合多出 {slug}.html（基线里没有）")

    # 块数量回归：基线里每条记的是该篇期望的结构块数量
    for slug, expected in sorted(baseline.items()):
        html = posts_dir / f"{slug}.html"
        if not html.exists() or not isinstance(expected, dict):
            continue
        actual = blockstats.blockstats(html)
        for key, want in expected.items():
            if actual.get(key) != want:
                errors.append(f"FAIL: {slug} 的 {key} 期望 {want}，实际 {actual.get(key)}")

    if vocab.exists():
        keys = vocab_keys(vocab)
        tag_files = {p.stem for p in (public / "tags").glob("*.html")} if (public / "tags").is_dir() else set()
        for key in keys:
            if key not in tag_files:
                errors.append(f"FAIL: 缺少 tags 归档页 {key}.html")
        if "index" not in tag_files:
            errors.append("FAIL: 缺少 tags 索引页 tags/index.html")
        extra = tag_files - set(keys) - {"index"}
        for name in sorted(extra):
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
        if re.search(r"<item>(?:(?!</item>).)*</item>", text, re.S) and "<category>" not in text:
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
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline.exists() else {}
    if not baseline:
        print(f"FAIL: 基线为空或不存在 {args.baseline}", file=sys.stderr)
        sys.exit(1)

    errors = verify_site(args.public, baseline, args.base_url, args.vocab)
    for error in errors:
        print(error)
    if errors:
        print(f"{len(errors)} 项失败")
        sys.exit(1)
    print(f"OK: {len(baseline)} 篇、{len(vocab_keys(args.vocab))} 个 tag、rss/sitemap 齐全")


if __name__ == "__main__":
    main()
