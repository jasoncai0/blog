#!/usr/bin/env python3
"""统计一篇文章 HTML 的结构块数量，用于迁移前后等价性比对。

用法: python3 scripts/blockstats.py <file.html>
输出: JSON {h1,h2,h3,table,figure,pre,hr}
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PATTERNS = {
    "h1": r"<h1[\s>]",
    "h2": r"<h2[\s>]",
    "h3": r"<h3[\s>]",
    "table": r"<table[\s>]",
    "figure": r"<figure[\s>]",
    "pre": r"<pre[\s>]",
    "hr": r"<hr[\s>]",
}


def blockstats(path: Path) -> dict[str, int]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    # <style>/<script> 里的选择器文本不应计入
    text = re.sub(r"<style\b.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<script\b.*?</script>", "", text, flags=re.S | re.I)
    return {k: len(re.findall(p, text, flags=re.I)) for k, p in PATTERNS.items()}


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: blockstats.py <file.html>", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(blockstats(Path(sys.argv[1]))), end="")


if __name__ == "__main__":
    main()
