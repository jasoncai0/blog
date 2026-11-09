#!/usr/bin/env python3
"""Tag 门禁：构建前拦截不合规的文章 front matter。

用法: python3 scripts/check-tags.py <posts_dir> <tags_yaml>
退出码: 0 通过；1 有违规（违规逐条打到 stderr）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MAX_TAGS = 4


def parse_vocab(path: Path) -> list[str]:
    """词表是 `key:` 顶格的二级 YAML；只取顶格 key，不引入 YAML 依赖。"""
    keys: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_-]*):\s*$", line)
        if match:
            keys.append(match.group(1))
    return keys


def split_front_matter(text: str) -> list[str] | None:
    lines = text.splitlines()
    # Markdown 源可能带 UTF-8 BOM
    if lines and lines[0].lstrip("\ufeff").strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return lines[1:index]
    return None


def parse_tags(front: list[str]) -> list[str] | None:
    for index, line in enumerate(front):
        match = re.match(r"^tags:\s*(.*)$", line)
        if not match:
            continue
        rest = match.group(1).strip()
        if rest.startswith("["):
            inner = rest[1 : rest.rfind("]")] if "]" in rest else rest[1:]
            quoted = re.findall(r'"([^"]*)"' + r"|'([^']*)'", inner)
            if quoted:
                return [a or b for a, b in quoted]
            return [part.strip() for part in inner.split(",") if part.strip()]
        values: list[str] = []
        for following in front[index + 1 :]:
            item = re.match(r"^\s+-\s+(.*)$", following)
            if item:
                values.append(item.group(1).strip().strip("'\""))
            elif following.strip():
                break
        return values
    return None


def check_tags(posts_dir: Path, vocab_path: Path) -> list[str]:
    vocab = set(parse_vocab(vocab_path))
    posts = sorted(path for path in posts_dir.rglob("*.md") if path.is_file())
    if not posts:
        return [f"{posts_dir}: 没有找到任何 .md 文章"]

    problems: list[str] = []
    for post in posts:
        name = post.relative_to(posts_dir)
        front = split_front_matter(post.read_text(encoding="utf-8"))
        if front is None:
            problems.append(f"{name}: 缺少 front matter")
            continue
        tags = parse_tags(front)
        if tags is None:
            problems.append(f"{name}: 缺少 tags 字段")
            continue
        if not tags:
            problems.append(f"{name}: tags 为空")
            continue
        if len(tags) > MAX_TAGS:
            problems.append(f"{name}: tags 超过 {MAX_TAGS} 个（{len(tags)}）")
        seen: set[str] = set()
        for tag in tags:
            if tag in seen:
                problems.append(f"{name}: tag 重复 `{tag}`")
            seen.add(tag)
            if tag not in vocab:
                problems.append(f"{name}: tag 不在词表内 `{tag}`")
    return problems


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: check-tags.py <posts_dir> <tags_yaml>", file=sys.stderr)
        sys.exit(2)
    posts_dir, vocab_path = Path(sys.argv[1]), Path(sys.argv[2])
    problems = check_tags(posts_dir, vocab_path)
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        print(f"{len(problems)} 处违规", file=sys.stderr)
        sys.exit(1)

    vocab = parse_vocab(vocab_path)
    used: set[str] = set()
    for post in posts_dir.rglob("*.md"):
        front = split_front_matter(post.read_text(encoding="utf-8")) or []
        used.update(parse_tags(front) or [])
    for orphan in sorted(set(vocab) - used):
        print(f"WARN: 词表里的 `{orphan}` 没有文章在用", file=sys.stderr)
    print(f"OK: {len(vocab)} 个 tag，全部文章合规")


if __name__ == "__main__":
    main()
