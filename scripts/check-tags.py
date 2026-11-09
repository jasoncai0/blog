#!/usr/bin/env python3
"""构建前门禁：tag 合规 + 正文短代码转义。

用法: python3 scripts/check-tags.py <posts_dir> <tags_yaml>
退出码: 0 通过；1 有违规（违规逐条打到 stderr）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MAX_TAGS = 4
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SHORTCODE_UNESCAPED = re.compile(r"\{\{[<%](?!\s*/\*)")


def parse_vocab(path: Path) -> tuple[list[str], list[str]]:
    """词表支持 `key:` 独占一行，或 `key: { name: ... }` 内联；返回 (合法 key, 非法 key)。"""
    valid: list[str] = []
    invalid: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_-]*):\s*(\{.*\}|)$", line)
        if not match:
            continue
        key = match.group(1)
        (valid if KEY_RE.match(key) else invalid).append(key)
    return valid, invalid


def split_front_matter(text: str) -> list[str] | None:
    lines = text.splitlines()
    if lines and lines[0].lstrip("\ufeff").strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return lines[1:index]
    return None


def split_items(inner: str) -> list[str]:
    """按逗号切分，但不动引号里的逗号；返回去掉引号与空白的项。"""
    items: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    for char in inner:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            buffer.append(char)
        elif char == ",":
            items.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    if buffer:
        items.append("".join(buffer))
    result: list[str] = []
    for item in items:
        item = item.strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'":
            item = item[1:-1]   # 引号内的空白属于值本身，不能顺手 trim
        if item:
            result.append(item)
    return result


def parse_tags(front: list[str]) -> list[str] | None:
    for index, line in enumerate(front):
        match = re.match(r"^tags:\s*(.*)$", line)
        if not match:
            continue
        rest = match.group(1).strip()
        if rest.startswith("["):
            inner = rest[1 : rest.rfind("]")] if "]" in rest else rest[1:]
            return split_items(inner)
        values: list[str] = []
        for following in front[index + 1 :]:
            item = re.match(r"^\s+-\s+(.*)$", following)
            if item:
                values.append(item.group(1).strip().strip("'\"").strip())
            elif following.strip():
                break
        return values
    return None


def check_shortcodes(text: str) -> list[str]:
    """Hugo 会执行代码块里的 {{< ... >}}，必须写成 {{</* ... */>}}。"""
    problems: list[str] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and SHORTCODE_UNESCAPED.search(line):
            problems.append(f"第 {number} 行：代码块里的短代码未转义（写成 {{{{</* ... */>}}）")
    return problems


def check_tags(posts_dir: Path, vocab_path: Path) -> list[str]:
    valid, invalid = parse_vocab(vocab_path)
    vocab = set(valid)
    posts = sorted(path for path in posts_dir.rglob("*.md") if path.is_file())
    problems: list[str] = [f"词表 key `{key}` 不是小写连字符形式" for key in invalid]
    if not posts:
        return problems + [f"{posts_dir}: 没有找到任何 .md 文章"]

    for post in posts:
        name = post.relative_to(posts_dir)
        text = post.read_text(encoding="utf-8")
        problems.extend(f"{name}: {issue}" for issue in check_shortcodes(text))

        front = split_front_matter(text)
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

    valid, _ = parse_vocab(vocab_path)
    used: set[str] = set()
    for post in posts_dir.rglob("*.md"):
        front = split_front_matter(post.read_text(encoding="utf-8")) or []
        used.update(parse_tags(front) or [])
    for orphan in sorted(set(valid) - used):
        print(f"WARN: 词表里的 `{orphan}` 没有文章在用", file=sys.stderr)
    print(f"OK: {len(valid)} 个 tag，全部文章合规")


if __name__ == "__main__":
    main()
