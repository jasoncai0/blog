import unittest
from pathlib import Path

CSS = Path(__file__).parent.parent / "assets" / "css" / "site.css"

REQUIRED = [
    "ul.posts",
    "ul.posts time",
    ".entry",
    ".summary",
    ".tagline",
    ".links",
    ".tag-chip",
]


class SiteCssTest(unittest.TestCase):
    def test_list_and_header_selectors_exist(self):
        """首页与 tag 归档页依赖这些类；缺了就会退化成浏览器默认列表。"""
        text = CSS.read_text(encoding="utf-8")
        missing = [selector for selector in REQUIRED if selector not in text]
        self.assertEqual(missing, [], f"site.css 缺少选择器：{missing}")
