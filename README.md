# blog

Jason Cai 的个人站点源码（https://www.jasoncai.site）。

Hugo 构建，本地出产物后 rsync 到服务器；服务器不装 Hugo。

- `content/posts/<slug>/index.md` — 文章（page bundle，图放 `diagrams/`）
- `data/tags.yaml` — tag 词表，唯一权威
- `scripts/` — 门禁、发布、校验
- `docs/superpowers/` — 设计与实施计划

写作与发布约定见 `docs/superpowers/specs/` 与仓库根 `AGENTS.md` 的「发文章」段。
