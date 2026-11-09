#!/usr/bin/env python3
"""把线上手工 HTML 反推成 Hugo Markdown（无源文章的迁移路径）。

用法:
  python3 scripts/reverse-convert.py --in a.html --out-dir content/posts/<slug> \
      --slug <slug> --meta meta.json [--assets DIR] [--demote-headings]
"""
from __future__ import annotations

import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path

VOID = {"br", "hr", "img", "meta", "link", "input", "source", "area", "base", "col", "embed", "wbr"}
SVG_RE = re.compile(r"<svg\b.*?</svg>", re.S)


class Node:
    __slots__ = ("tag", "attrs", "children", "data")

    def __init__(self, tag=None, attrs=None, children=None, data=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = children if children is not None else []
        self.data = data

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())


class TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(Node(data=data))

    def handle_comment(self, data):
        match = re.fullmatch(r"SVG:(\d+)", data.strip())
        if match:
            self.stack[-1].children.append(Node("svgref", {"n": match.group(1)}))


def walk(node):
    for child in node.children:
        yield child
        if child.tag is not None:
            yield from walk(child)


def find(node, tag):
    for child in node.children:
        if child.tag == tag:
            return child
        if child.tag is not None:
            found = find(child, tag)
            if found is not None:
                return found
    return None


def raw_text(node) -> str:
    parts = []
    for child in node.children:
        parts.append(child.data if child.data is not None else raw_text(child))
    return "".join(parts)


def escape_shortcodes(text: str) -> str:
    """Hugo 会执行代码块里的 {{< ... >}}，按官方转义写成 {{</* ... */>}}。"""
    text = text.replace("{{<", "{{</*").replace(">}}", "*/}}")
    text = text.replace("{{%", "{{%/*").replace("%}}", "*/%}}")
    return text


def inline(node) -> str:
    parts = []
    for child in node.children:
        if child.data is not None:
            parts.append(child.data)
            continue
        tag = child.tag
        if tag in ("strong", "b"):
            parts.append(f"**{inline(child)}**")
        elif tag in ("em", "i"):
            parts.append(f"*{inline(child)}*")
        elif tag == "code":
            parts.append("`" + raw_text(child).replace("`", "\\`") + "`")
        elif tag == "a":
            parts.append(f"[{inline(child)}]({child.attrs.get('href', '')})")
        elif tag == "br":
            parts.append(" ")
        elif tag == "img":
            parts.append(f"![{child.attrs.get('alt', '')}]({child.attrs.get('src', '')})")
        else:
            parts.append(inline(child))
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def table_to_md(table) -> str:
    rows: list[list[str]] = []
    for node in walk(table):
        if node.tag != "tr":
            continue
        cells = [inline(cell) or " " for cell in node.children if cell.tag in ("th", "td")]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = len(rows[0])
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join([" --- "] * width) + "|"]
    for row in rows[1:]:
        row = (row + [" "] * width)[:width]
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
    return "\n".join(lines)


class Converter:
    def __init__(self, svgs: list[str], assets: Path | None, demote: bool):
        self.svgs = svgs
        self.assets = assets
        self.demote = demote
        self.diagrams: list[tuple[str, str]] = []
        self.counter = 0
        self.skip_first_h1 = True

    def diagram(self, svg: str) -> str:
        self.counter += 1
        name = f"{self.counter:02d}.svg"
        self.diagrams.append((name, svg))
        return name

    def figure(self, node) -> list[str]:
        svgref = next((n for n in walk(node) if n.tag == "svgref"), None)
        if svgref is None:
            return []
        svg = self.svgs[int(svgref.attrs["n"])]
        caption = ""
        for child in node.children:
            if child.tag == "figcaption":
                caption = re.sub(r"^图\s*\d+\s*·?\s*", "", inline(child)).strip()
                caption = caption.replace('"', "\u201d").replace("\n", " ")
        return [f'{{{{< fig "diagrams/{self.diagram(svg)}" "{caption}" >}}}}']

    def image(self, node) -> list[str]:
        src = node.attrs.get("src", "")
        alt = node.attrs.get("alt", "")
        if not src.endswith(".svg") or self.assets is None:
            return [f"![{alt}]({src})"]
        candidate = self.assets / src.lstrip("/")
        if not candidate.exists():
            return [f"![{alt}]({src})"]
        return [f'{{{{< fig "diagrams/{self.diagram(candidate.read_text(encoding="utf-8"))}" "{alt}" >}}}}']

    def blocks(self, nodes) -> list[str]:
        out: list[str] = []
        for node in nodes:
            out.extend(self.block(node))
        return out

    def block(self, node) -> list[str]:
        if node.data is not None:
            text = node.data.strip()
            return [text] if text else []
        tag = node.tag
        if tag is None:
            return []

        if tag in ("h1", "h2", "h3", "h4"):
            level = int(tag[1])
            if level == 1 and self.skip_first_h1:
                self.skip_first_h1 = False
                return []
            if self.demote:
                level = min(level + 1, 4)
            return ["#" * level + " " + inline(node)]

        if tag == "p":
            if node.classes & {"back", "meta", "post-meta", "toc-title", "copyright"}:
                return []
            images = [n for n in walk(node) if n.tag == "img"]
            text_only = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", inline(node))
            text_only = re.sub(r"\[\]\([^)]*\)", "", text_only).strip()
            if images and not text_only:
                lines: list[str] = []
                for image in images:
                    lines.extend(self.image(image))
                return lines
            text = inline(node)
            return [text] if text else []

        if tag == "blockquote":
            inner = self.blocks(node.children)
            text = "\n".join(inner)
            return ["\n".join("> " + line if line else ">" for line in text.splitlines())] if text else []

        if tag in ("ul", "ol"):
            lines = []
            for index, item in enumerate(node.children, 1):
                if item.tag != "li":
                    continue
                marker = "-" if tag == "ul" else f"{index}."
                lines.append(f"{marker} {inline(item)}")
            return ["\n".join(lines)] if lines else []

        if tag == "pre":
            lang = node.attrs.get("data-lang", "")
            body = raw_text(node).strip()
            return ["\n".join(["```" + lang, escape_shortcodes(body), "```"])]

        if tag == "hr":
            return ["---"]

        if tag == "table":
            text = table_to_md(node)
            return [text] if text else []

        if tag == "figure":
            return self.figure(node)

        if tag == "img":
            return self.image(node)

        if tag == "nav" and "toc" in node.classes:
            return []

        if tag in ("footer",):
            return []

        if tag == "div" and "table-wrap" in node.classes:
            table = find(node, "table")
            text = table_to_md(table) if table is not None else ""
            return [text] if text else []

        if tag == "svgref":
            return []

        return self.blocks(node.children)


def html_to_md(html: str, assets: Path | None = None, demote: bool = False):
    svgs = SVG_RE.findall(html)
    placeholder = lambda m: f"<!--SVG:{svgs.index(m.group(0))}-->"
    stripped = SVG_RE.sub(placeholder, html)
    stripped = re.sub(r"<(style|script)\b.*?</\1>", " ", stripped, flags=re.S | re.I)

    parser = TreeParser()
    parser.feed(stripped)
    parser.close()

    body = find(parser.root, "body")
    container = body or parser.root
    main = find(container, "main")
    content = main or container

    converter = Converter(svgs, assets, demote)
    lines = converter.blocks(content.children)
    md = "\n\n".join(line for line in lines if line is not None)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md + "\n", converter.diagrams


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="source", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--meta", required=True, type=Path)
    parser.add_argument("--slug", default=None)
    parser.add_argument("--assets", type=Path, default=None)
    parser.add_argument("--demote-headings", action="store_true")
    args = parser.parse_args()

    md, diagrams = html_to_md(
        args.source.read_text(encoding="utf-8"), args.assets, args.demote_headings
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if diagrams:
        target = args.out_dir / "diagrams"
        target.mkdir(exist_ok=True)
        for name, svg in diagrams:
            (target / name).write_text(svg, encoding="utf-8")
    meta = json.loads(args.meta.read_text(encoding="utf-8"))
    if args.slug:
        meta["slug"] = args.slug
    front = ["---"]
    for key in ("title", "date", "summary", "description"):
        if meta.get(key):
            value = str(meta[key]).replace('"', '\\"')
            front.append(f'{key}: "{value}"')
    front.append("tags: [" + ", ".join(f'"{t}"' for t in meta["tags"]) + "]")
    front.append(f'slug: "{meta["slug"]}"')
    front.append("---")
    (args.out_dir / "index.md").write_text(
        "\n".join(front) + "\n\n" + md, encoding="utf-8"
    )
    print(f"converted {meta['slug']}: {len(md)} chars, {len(diagrams)} diagrams")


if __name__ == "__main__":
    main()
