import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "blockstats", Path(__file__).parent.parent / "scripts" / "blockstats.py"
)
blockstats = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(blockstats)


class BlockstatsTest(unittest.TestCase):
    def stats(self, html: str) -> dict:
        path = Path(tempfile.mkdtemp()) / "a.html"
        path.write_text(html, encoding="utf-8")
        return blockstats.blockstats(path)

    def test_counts_core_blocks(self):
        got = self.stats(
            "<h1>t</h1><h2>a</h2><h2>b</h2><h3>c</h3>"
            "<table></table><figure></figure><figure></figure><pre></pre><hr>"
        )
        self.assertEqual(
            got, {"h1": 1, "h2": 2, "h3": 1, "table": 1, "figure": 2, "pre": 1, "hr": 1}
        )

    def test_ignores_stylesheet_and_script_text(self):
        got = self.stats("<style>h2{}</style><script>var h2;</script><h2>x</h2>")
        self.assertEqual(got["h2"], 1)
