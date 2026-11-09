#!/usr/bin/env python3
"""把有 Markdown 源的文章迁成 Hugo page bundle。

用法:
  python3 scripts/migrate_sourced.py --manifest .tmp/manifest.json --out content/posts

manifest 是 JSON 数组，每项:
  {"slug","src","title","date","summary","description"?,"tags"[]}
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path


def yaml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_front_matter(meta: dict) -> str:
    lines = ["---"]
    lines.append(f"title: {yaml_str(meta['title'])}")
    lines.append(f"date: {yaml_str(str(meta['date']))}")
    lines.append(f"summary: {yaml_str(meta['summary'])}")
    if meta.get("description"):
        lines.append(f"description: {yaml_str(meta['description'])}")
    tags = ", ".join(yaml_str(tag) for tag in meta["tags"])
    lines.append(f"tags: [{tags}]")
    lines.append(f"slug: {yaml_str(meta['slug'])}")
    lines.append("---")
    return "\n".join(lines)


FIGURE_RE = re.compile(r"^!\[(.*?)\]\((.*?)\)\s*$")


def convert_body(source: str) -> str:
    lines = source.splitlines()
    # 去掉首个一级标题（模板已渲染 title）
    for index, line in enumerate(lines):
        if line.startswith("# "):
            del lines[index]
            if index < len(lines) and not lines[index].strip():
                del lines[index]
            break
    out = []
    for line in lines:
        match = FIGURE_RE.match(line.strip())
        if match:
            alt, src = match.group(1), match.group(2)
            out.append(f'{{{{< fig "{src}" "{alt}" >}}}}')
        else:
            out.append(line)
    return "\n".join(out).lstrip("\n")


def migrate_one(src_md: Path, out_dir: Path, meta: dict) -> Path:
    bundle = out_dir / meta["slug"]
    bundle.mkdir(parents=True, exist_ok=True)

    diagrams = src_md.parent / "diagrams"
    if diagrams.is_dir():
        target = bundle / "diagrams"
        target.mkdir(exist_ok=True)
        for svg in diagrams.glob("*.svg"):
            shutil.copy2(svg, target / svg.name)

    body = convert_body(src_md.read_text(encoding="utf-8"))
    document = f"{render_front_matter(meta)}\n\n{body}\n"
    out_file = bundle / "index.md"
    out_file.write_text(document, encoding="utf-8")
    return out_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    entries = json.loads(args.manifest.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    for meta in entries:
        out = migrate_one(Path(meta["src"]).expanduser(), args.out, meta)
        print(f"migrated {meta['slug']} -> {out}")


if __name__ == "__main__":
    main()
