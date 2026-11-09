import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "verify_site", Path(__file__).parent.parent / "scripts" / "verify-site.py"
)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)

VOCAB = "meta:\n  name: 随笔\nagent:\n  name: Agent\n"


class VerifySiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.public = self.tmp / "public"
        (self.public / "posts").mkdir(parents=True)
        self.vocab = self.tmp / "tags.yaml"
        self.vocab.write_text(VOCAB, encoding="utf-8")
        self.content = self.tmp / "content"
        (self.content / "posts" / "a").mkdir(parents=True)
        (self.content / "posts" / "a" / "index.md").write_text(
            '---\ntitle: "a"\ntags: ["meta"]\n---\n', encoding="utf-8")

    def check(self, baseline, notes=None):
        return verify.verify_site(self.public, baseline, vocab=self.vocab,
                                  content=self.content, notes=notes)

    def make_complete(self, slugs=("a",), tags=("index", "meta")):
        for slug in slugs:
            (self.public / "posts" / f"{slug}.html").write_text("<h2>x</h2>", encoding="utf-8")
        (self.public / "tags").mkdir(exist_ok=True)
        for name in tags:
            (self.public / "tags" / f"{name}.html").write_text("x", encoding="utf-8")
        items = "".join(f"<item><category>m</category></item>" for _ in slugs)
        (self.public / "rss.xml").write_text(
            f"<rss><channel>{items}</channel></rss>", encoding="utf-8")
        (self.public / "sitemap.xml").write_text("<urlset/>", encoding="utf-8")

    def test_detects_missing_tag_page(self):
        (self.public / "posts" / "a.html").write_text("<h1>a</h1>", encoding="utf-8")
        errors = self.check({"a": {"h2": 1}})
        self.assertTrue(any("tags" in e for e in errors), errors)

    def test_detects_block_count_drift(self):
        (self.public / "posts" / "a.html").write_text("<h2>x</h2><h2>y</h2><h2>z</h2>",
                                                     encoding="utf-8")
        errors = self.check({"a": {"h2": 1}})
        self.assertTrue(any("h2" in e for e in errors), errors)

    def test_content_post_without_output_is_error(self):
        (self.content / "posts" / "ghost").mkdir(parents=True)
        (self.content / "posts" / "ghost" / "index.md").write_text(
            '---\ntitle: "g"\ntags: ["meta"]\n---\n', encoding="utf-8")
        errors = self.check({"a": {"h2": 1}})
        self.assertTrue(any("ghost" in e for e in errors), errors)

    def test_new_post_not_in_baseline_is_a_note_not_error(self):
        self.make_complete()
        notes = []
        errors = self.check({}, notes=notes)
        self.assertEqual(errors, [])
        self.assertTrue(any("a" in n for n in notes), notes)

    def test_detects_rss_without_category(self):
        self.make_complete()
        (self.public / "rss.xml").write_text(
            "<rss><channel><item><title>a</title></item></channel></rss>", encoding="utf-8")
        errors = self.check({"a": {"h2": 1}})
        self.assertTrue(any("category" in e for e in errors), errors)

    def test_update_baseline_records_current_counts(self):
        (self.public / "posts" / "a.html").write_text("<h2>x</h2><h2>y</h2>", encoding="utf-8")
        target = self.tmp / "baseline.json"
        verify.update_baseline(self.public, target)
        self.assertEqual(json.loads(target.read_text())["a"]["h2"], 2)
