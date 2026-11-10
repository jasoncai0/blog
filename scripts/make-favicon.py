#!/usr/bin/env python3
"""生成站点图标：墨色圆角方块 + 纸白衬线 J。

配色取自 assets/css/site.css（--ink #2d3142 / --paper #f5f5f5）。
输出：static/favicon.svg、static/favicon.ico（16/32/48）、
      static/favicon-32.png、static/favicon-512.png、static/apple-touch-icon.png
用法：python3 scripts/make-favicon.py（需要 Pillow）
"""
import math
import pathlib
import sys

from PIL import Image, ImageDraw

INK = (45, 49, 66, 255)
PAPER = (245, 245, 245, 255)
VIEW = 64.0
RADIUS = 14.0

STROKE = 8.0
STEM_X = 41.0
TOP_Y = 15.0
BEND_Y = 41.0
HOOK_R = 9.5
SERIF_X0 = 33.0      # 顶部衬线只向左侧伸出，像 Times 的 J
SERIF_X1 = 45.0

SVG_PATH = (
    f'M{SERIF_X0:g} {TOP_Y:g} H{SERIF_X1:g} '                      # 顶部衬线
    f'M{STEM_X:g} {TOP_Y:g} V{BEND_Y:g} '                          # 竖干
    f'A{HOOK_R:g} {HOOK_R:g} 0 0 1 {STEM_X - 2 * HOOK_R:g} {BEND_Y:g}'  # 底部弯钩
)
SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="jasoncai — 蔡振声的博客">
  <rect width="64" height="64" rx="{RADIUS:g}" fill="#2d3142"/>
  <path d="{SVG_PATH}" fill="none" stroke="#f5f5f5" stroke-width="{STROKE:g}"
        stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


def _stamp(d: ImageDraw.ImageDraw, pts, r: float) -> None:
    """沿路径盖圆点：比 line(width=) 更稳，端点天然是圆头。"""
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=PAPER)


def render(size: int) -> Image.Image:
    ss = 8 if size < 400 else 4
    s = size * ss
    k = s / VIEW
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=RADIUS * k, fill=INK)

    r = STROKE * k / 2
    step = max(1, int(0.6 * k))
    # 顶部衬线
    _stamp(d, [(x * k, TOP_Y * k) for x in _frange(SERIF_X0, SERIF_X1, 0.25)], r)
    # 竖干
    _stamp(d, [(STEM_X * k, y * k) for y in _frange(TOP_Y, BEND_Y, 0.25)], r)
    # 弯钩：0°→180°，穿过底部
    cx = (STEM_X - HOOK_R) * k
    cy = BEND_Y * k
    hook = []
    for y in _frange(TOP_Y, BEND_Y, 0.25):
        pass
    for i in range(0, 181, 1):
        a = math.radians(i)
        hook.append((cx + HOOK_R * k * math.cos(a), cy + HOOK_R * k * math.sin(a)))
    _stamp(d, hook[::step], r)
    return img.resize((size, size), Image.LANCZOS)


def _frange(a: float, b: float, step: float):
    x = a
    while x < b:
        yield x
        x += step
    yield b


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    (root / "static").mkdir(exist_ok=True)
    (root / "static/favicon.svg").write_text(SVG, encoding="utf-8")
    render(512).save(root / "static/favicon-512.png")
    render(180).save(root / "static/apple-touch-icon.png")
    render(32).save(root / "static/favicon-32.png")
    render(48).save(root / "static/favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print("written static/favicon.{svg,ico} favicon-32.png apple-touch-icon.png favicon-512.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
