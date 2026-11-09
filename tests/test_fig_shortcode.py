import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent


class FigShortcodeTest(unittest.TestCase):
    def test_fig_shortcode_fails_on_missing_svg(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "content" / "posts").mkdir(parents=True)
            (site / "layouts" / "shortcodes").mkdir(parents=True)
            # 用仓库里真实的 shortcode，否则测到的是「模板不存在」而非「缺图」
            shutil.copy(REPO / "layouts" / "shortcodes" / "fig.html",
                        site / "layouts" / "shortcodes" / "fig.html")
            (site / "hugo.toml").write_text(
                'baseURL = "https://example.com/"\nuglyURLs = true\n', encoding="utf-8"
            )
            (site / "layouts" / "single.html").write_text("{{ .Content }}", encoding="utf-8")
            (site / "content" / "posts" / "a.md").write_text(
                '---\ntitle: "A"\ntags: ["meta"]\n---\n\n{{< fig "diagrams/nope.svg" "缺图" >}}\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                ["hugo", "--source", str(site)], capture_output=True, text=True
            )
            self.assertNotEqual(result.returncode, 0, "缺图应导致构建失败")
            self.assertIn("nope.svg", result.stderr + result.stdout)
