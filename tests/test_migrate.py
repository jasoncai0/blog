import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "migrate_sourced", Path(__file__).parent.parent / "scripts" / "migrate_sourced.py"
)
migrate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migrate)


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src_dir = self.tmp / "src"
        self.src_dir.mkdir()
        self.out = self.tmp / "content" / "posts"
        self.out.mkdir(parents=True)

    def test_slug_from_filename_not_title(self):
        src = self.src_dir / "opencode-plan.md"
        src.write_text("# 规划：A/B 与 C\n\n正文\n", encoding="utf-8")
        out = migrate.migrate_one(
            src,
            self.out,
            meta={
                "slug": "opencode-plan",
                "title": "规划：A/B 与 C",
                "date": "2026-11-09",
                "summary": "s",
                "tags": ["opencode"],
            },
        )
        self.assertEqual(out.parent.name, "opencode-plan")
        self.assertEqual(out.name, "index.md")
        text = out.read_text(encoding="utf-8")
        self.assertIn('slug: "opencode-plan"', text)

    def test_h1_removed_and_fig_converted(self):
        (self.src_dir / "diagrams").mkdir()
        (self.src_dir / "diagrams" / "01.svg").write_text("<svg/>", encoding="utf-8")
        src = self.src_dir / "x.md"
        src.write_text("# 标题\n\n![委派](diagrams/01.svg)\n\n正文\n", encoding="utf-8")
        out = migrate.migrate_one(
            src,
            self.out,
            meta={
                "slug": "x",
                "title": "标题",
                "date": "2026-11-03",
                "summary": "摘要",
                "tags": ["opencode", "multi-agent"],
            },
        )
        text = out.read_text(encoding="utf-8")
        self.assertNotIn("\n# 标题", text)
        self.assertIn('{{< fig "diagrams/01.svg" "委派" >}}', text)
        self.assertTrue((out.parent / "diagrams" / "01.svg").exists())

    def test_front_matter_has_required_fields(self):
        src = self.src_dir / "y.md"
        src.write_text("# Y\n\n正文\n", encoding="utf-8")
        out = migrate.migrate_one(
            src,
            self.out,
            meta={
                "slug": "y",
                "title": "Y",
                "date": "2026-11-01",
                "summary": "一句摘要",
                "description": "meta 文案",
                "tags": ["meta"],
            },
        )
        text = out.read_text(encoding="utf-8")
        for field in ('title: "Y"', 'date: "2026-11-01"', 'summary: "一句摘要"',
                      'description: "meta 文案"', 'tags: ["meta"]', 'slug: "y"'):
            self.assertIn(field, text)
