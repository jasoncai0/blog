import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "reverse_convert", Path(__file__).parent.parent / "scripts" / "reverse-convert.py"
)
reverse = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reverse)


class ReverseConvertTest(unittest.TestCase):
    def test_code_block_with_shortcode_syntax_is_escaped(self):
        html = "<pre><code>{{&lt; fig &quot;a.svg&quot; &gt;}}</code></pre>"
        md, _ = reverse.html_to_md(html)
        self.assertIn("```", md)
        # Hugo 会执行代码块里的 {{< ... >}}，必须转义成 {{</* ... */>}}
        self.assertIn("{{</* fig", md)
        self.assertIn("*/}}", md)

    def test_figure_inlined_svg_becomes_shortcode(self):
        html = (
            '<figure id="fig-1"><div class="diagram"><svg><rect/></svg></div>'
            '<figcaption><span class="fig-no">图 1</span>说明</figcaption></figure>'
        )
        md, diagrams = reverse.html_to_md(html)
        self.assertEqual(len(diagrams), 1)
        self.assertIn('{{< fig "diagrams/01.svg" "说明" >}}', md)
        self.assertIn("<svg>", diagrams[0][1])

    def test_table_becomes_pipe_table(self):
        html = (
            "<div class=\"table-wrap\"><table><thead><tr><th>甲</th><th>乙</th></tr></thead>"
            "<tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>"
        )
        md, _ = reverse.html_to_md(html)
        self.assertIn("| 甲 | 乙 |", md)
        self.assertIn("| 1 | 2 |", md)

    def test_inline_markup_round_trips(self):
        html = "<p>看 <code>foo()</code> 与 <strong>重点</strong> 和 <a href='/x'>链接</a>。</p>"
        md, _ = reverse.html_to_md(html)
        self.assertIn("`foo()`", md)
        self.assertIn("**重点**", md)
        self.assertIn("[链接](/x)", md)
