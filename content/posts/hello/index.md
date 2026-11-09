---
title: "这个博客是怎么搭起来的"
date: "2026-11-02T20:12:00+08:00"
summary: "一台腾讯云北京的轻量服务器，nginx 托管静态文件，Let's Encrypt 的证书，没有数据库，没有后台。 写作流程是：本地 posts/ 下新建一个 Markdown 文件，跑 ./publish.sh，构建成 …"
description: "一台腾讯云北京的轻量服务器，nginx 托管静态文件，Let's Encrypt 的证书，没有数据库，没有后台。 写作流程是：本地 posts/ 下新建一个 Markdown 文件，跑 ./publish.sh，构建成 …"
tags: ["meta"]
slug: "hello"
---

一台腾讯云北京的轻量服务器，nginx 托管静态文件，Let's Encrypt 的证书，没有数据库，没有后台。

写作流程是：本地 `posts/` 下新建一个 Markdown 文件，跑 `./publish.sh`，构建成 HTML 后 rsync 到服务器。整个站点加起来不到两百行代码。

## 为什么最后还是用了框架

起初是一个 Python 脚本：几十篇 Markdown 变几十个 HTML，主题、插件、增量构建都不需要。这个判断在只有一列文章的时候成立。

加了 tag 之后不成立。tag 索引页、每个 tag 的归档页、RSS 里的 category、sitemap —— 这些不再是「渲染一篇文档」，而是「维护一组互相引用的页面」。自己写不是写不出来，是要养：每次改归档样式、改条数统计、改 RSS 字段，都得回到那个脚本里改，而且没有别人看得懂它。

于是迁到 Hugo。taxonomy 是它内置的：只要 front matter 里写了 `tags`，索引页、归档页、RSS 里的 category 自己会长出来。脚本只剩三件事 —— 发布前校验 tag、构建、上传。

## 现在怎么发一篇

1. `hugo new posts/<slug>.md`，archetype 自带一个空的 `tags`
2. 填 `title` / `date` / `summary` / `tags`；tag 只能取 `data/tags.yaml` 里的词，漏写或写错，门禁直接拦下
3. `scripts/publish.sh` 一条命令完成校验、构建、备份、上传、线上复验

## 目录结构

```
blog/
├── content/posts/<slug>/index.md   # 文章（page bundle，图放 diagrams/）
├── data/tags.yaml                  # tag 词表，唯一权威
├── layouts/                        # 模板
├── assets/css/site.css             # 纸感主题
└── scripts/                        # 门禁、发布、校验
```

服务器那边没变：还是 nginx 托管一堆静态文件，没有数据库，没有后台。
