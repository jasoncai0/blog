import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "check_tags", Path(__file__).parent.parent / "scripts" / "check-tags.py"
)
check_tags = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_tags)

VOCAB = "meta:\n  name: 随笔\nagent:\n  name: Agent\n"


def write(post_dir: Path, name: str, front: str) -> None:
    (post_dir / name).write_text(f"---\n{front}\n---\n正文\n", encoding="utf-8")


class CheckTagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "vocab.yaml").write_text(VOCAB, encoding="utf-8")
        self.posts = self.tmp / "posts"
        self.posts.mkdir()

    def check(self):
        return check_tags.check_tags(self.posts, self.tmp / "vocab.yaml")

    def test_missing_tags_rejected(self):
        write(self.posts, "a.md", 'title: "A"\ndate: 2026-11-09')
        self.assertTrue(self.check())

    def test_unknown_tag_rejected(self):
        write(self.posts, "a.md", 'title: "A"\ntags: ["nope"]')
        self.assertTrue(self.check())

    def test_more_than_four_rejected(self):
        write(self.posts, "a.md", 'title: "A"\ntags: ["meta","agent","meta","agent","meta"]')
        self.assertTrue(self.check())

    def test_case_and_whitespace_rejected(self):
        write(self.posts, "a.md", 'title: "A"\ntags: ["Meta"]')
        self.assertTrue(self.check())
        write(self.posts, "a.md", 'title: "A"\ntags: [" meta "]')
        self.assertTrue(self.check())

    def test_valid_post_passes(self):
        write(self.posts, "a.md", 'title: "A"\ntags: ["meta", "agent"]')
        self.assertEqual(self.check(), [])

    def test_empty_posts_dir_rejected(self):
        self.assertTrue(self.check())


class ParserAndEscapeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vocab = self.tmp / "vocab.yaml"
        self.vocab.write_text(VOCAB, encoding="utf-8")
        self.posts = self.tmp / "posts"
        self.posts.mkdir()

    def check(self):
        return check_tags.check_tags(self.posts, self.vocab)

    def test_mixed_quotes_keeps_all_tags(self):
        write(self.posts, "a.md", 'title: "A"\ntags: [Meta, "agent"]')
        problems = self.check()
        self.assertTrue(any("Meta" in p for p in problems), problems)

    def test_inline_vocab_entry_accepted(self):
        self.vocab.write_text("meta:\n  name: 随笔\nnewtag: { name: 新, description: d }\n", encoding="utf-8")
        write(self.posts, "a.md", 'title: "A"\ntags: ["newtag"]')
        self.assertEqual(self.check(), [])

    def test_uppercase_vocab_key_rejected(self):
        self.vocab.write_text("meta:\n  name: 随笔\nOpenCode:\n  name: x\n", encoding="utf-8")
        write(self.posts, "a.md", 'title: "A"\ntags: ["meta"]')
        problems = self.check()
        self.assertTrue(any("OpenCode" in p for p in problems), problems)

    def test_shortcode_in_code_block_rejected(self):
        (self.posts / "a.md").write_text(
            '---\ntitle: "A"\ntags: ["meta"]\n---\n\n```\n{{< fig "x.svg" "y" >}}\n```\n',
            encoding="utf-8")
        problems = self.check()
        self.assertTrue(any("短代码" in p or "shortcode" in p for p in problems), problems)

    def test_escaped_shortcode_in_code_block_passes(self):
        (self.posts / "a.md").write_text(
            '---\ntitle: "A"\ntags: ["meta"]\n---\n\n```\n{{</* fig "x.svg" "y" */>}}\n```\n',
            encoding="utf-8")
        self.assertEqual(self.check(), [])
