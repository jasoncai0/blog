---
title: "这个博客是怎么搭起来的"
date: "2026-11-02"
summary: "一台腾讯云北京的轻量服务器，nginx 托管静态文件，Let's Encrypt 的证书，没有数据库，没有后台。 写作流程是：本地 posts/ 下新建一个 Markdown 文件，跑 ./publish.sh，构建成 …"
description: "一台腾讯云北京的轻量服务器，nginx 托管静态文件，Let's Encrypt 的证书，没有数据库，没有后台。 写作流程是：本地 posts/ 下新建一个 Markdown 文件，跑 ./publish.sh，构建成 …"
tags: ["meta"]
slug: "hello"
---

一台腾讯云北京的轻量服务器，nginx 托管静态文件，Let's Encrypt 的证书，没有数据库，没有后台。

写作流程是：本地 `posts/` 下新建一个 Markdown 文件，跑 `./publish.sh`，构建成 HTML 后 rsync 到服务器。整个站点加起来不到两百行代码。

## 为什么不用现成的

Hugo、Hexo、Astro 都试过，它们解决的是我没有的问题 —— 主题系统、插件生态、增量构建。我只需要把几十篇 Markdown 变成几十个 HTML。

一个 Python 脚本就够了，而且我完全知道它在做什么。

## 目录结构

```
blog/
├── posts/        # Markdown 原文
├── static/       # CSS、图片
├── build.py      # 构建
└── publish.sh    # 构建 + 上传
```

就这样。
