import importlib.util
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

    def test_detects_missing_tag_page(self):
        (self.public / "posts" / "a.html").write_text("<h1>a</h1>", encoding="utf-8")
        errors = verify.verify_site(self.public, {"a": {"h2": 1}}, vocab=self.vocab, content=self.content)
        self.assertTrue(any("tags" in e for e in errors))

    def test_detects_post_count_mismatch(self):
        (self.public / "posts" / "a.html").write_text("<h1>a</h1>", encoding="utf-8")
        errors = verify.verify_site(
            self.public, {"a": {"h2": 1}, "b": {"h2": 2}}, vocab=self.vocab
        )
        self.assertTrue(any("b" in e for e in errors))

    def test_detects_rss_without_category(self):
        (self.public / "posts" / "a.html").write_text("<h1>a</h1>", encoding="utf-8")
        (self.public / "tags").mkdir()
        (self.public / "tags" / "index.html").write_text("x", encoding="utf-8")
        (self.public / "tags" / "meta.html").write_text("x", encoding="utf-8")
        (self.public / "tags" / "agent.html").write_text("x", encoding="utf-8")
        (self.public / "rss.xml").write_text(
            "<rss><channel><item><title>a</title></item></channel></rss>", encoding="utf-8"
        )
        (self.public / "sitemap.xml").write_text("<urlset/>", encoding="utf-8")
        errors = verify.verify_site(self.public, {"a": {"h2": 1}}, vocab=self.vocab, content=self.content)
        self.assertTrue(any("category" in e for e in errors))
