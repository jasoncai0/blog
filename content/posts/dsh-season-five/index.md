---
title: "第五季：容器与现场"
date: "2026-11-17T18:00:00+08:00"
summary: "前四季讲「东西怎么工作」。第五季往下挖两层：插件凭什么能被装进去、被干净拆掉；崩了之后磁盘上剩下什么、怎么从日志还原结论。四篇，附两张图。"
description: "DSH 深挖第五季导览：Cordis 容器、三层组装、落盘语义、现场诊断，以及两条阅读路径。"
tags: ["harness","architecture","meta"]
slug: "dsh-season-five"
---

> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46）。本季行号以该 commit 的工作树为准；凡未做运行时验证的结论，正文里会写明推断依据。

前四季分别讲请求生命周期、主干两侧、工具族与运行时、横切机制。每一季都在问「这个东西怎么工作」。

还有一类问题更靠下：插件凭什么能插进去、凭什么能被干净地拆掉；进程崩了之后磁盘上剩下什么、能不能据此还原出结论。

第五季就写这两件事，四篇。

{{< fig "diagrams/01-map.svg" "第五季四篇分两带：容器两篇讲「怎么装起来」，现场两篇讲「坏了怎么看」。" >}}

## 1. 前四季留下了什么

| 季 | 篇数 | 视角 | 没回答的问题 |
| --- | --- | --- | --- |
| 第一季 | 21 | 请求生命周期（输入→循环→工具→委派→记忆） | 插件系统本身 |
| 第二季 | 11 | 主干两侧（模型、客户端、执行环境、MCP、宿主、协议） | 组装与启动 |
| 第三季 | 13 | 工具族与运行时底座 | 磁盘与诊断 |
| 第四季 | 11 | 横切机制（消息、兼容、错误、并发、实验面） | 容器与效果 |

第五季补的正是最后一列：**容器怎么跑起来，坏了去哪看**。

## 2. 四篇

| # | 篇目 | 视角 | 回答 |
| --- | --- | --- | --- |
| 1 | [Cordis 内部](/posts/dsh-cordis-internals.html) | 容器 | fiber 六态、effect 逆序释放、服务可见性、事件派发 |
| 2 | [三层组装](/posts/dsh-bundle-assembly.html) | 容器 | profile、bundle、patch 如何叠成一份配置 |
| 3 | [落盘](/posts/dsh-data-at-rest.html) | 现场 | 句柄、flush 屏障、撕裂尾部、修复归属 |
| 4 | [现场诊断](/posts/dsh-diagnostics.html) | 现场 | 从会话文件到一行结论的取证路径 |

依赖上，第 1 篇是其余三篇的前提：不理解 fiber 与 effect，profile/patch 的层叠会读成「配置文件优先级」，落盘的「谁来修复」也会读成实现细节。

## 3. 两条路径

{{< fig "diagrams/02-paths.svg" "两条路径：做插件从容器读起，排故障从现场读起；两条都收在日志这一层。" >}}

| 路径 | 适合 | 顺序 |
| --- | --- | --- |
| 做插件 | 想加拦截、换实现、加工具 | Cordis 内部 → 三层组装 → 写插件（第三季） |
| 排故障 | 线上行为不对 | 现场诊断 → 落盘 → 错误地图（第四季）→ 会话即日志（第一季） |

两条路径的交点是会话日志：容器决定「谁能注册」，日志决定「发生过什么」。

## 4. 这一季的三个约定

**证据分级。** 每篇开头写清依据：源码、文档、测试、运行时观察。只有前两类时，结论会标成推断。

**行号可追。** 所有 `file:line` 都取自 commit `fcc0ad46`；这个版本之后行号会漂，按符号名搜比按行号搜稳。

**图只画结构。** 四张图都是 960×600 级别的结构图，不画真实数据；图里的文字是结论，不是示例。

## 5. 源码索引

| 位置 | 内容 |
| --- | --- |
| `docs/architecture.zh.md` | 组合、事件域、扩展点总表 |
| `docs/glossary.zh.md` | 术语：fiber、effect、seam、service |
| `vendor/cordis/src/` | 容器实现（9 文件 2,811 行） |
| `packages/bundle/base/cordis.patch.yml` | 装配语义与行形状 |
| `docs/subsystems/persistence.zh.md` | 句柄、flush、崩溃修复 |
| `docs/subsystems/session.zh.md` | 事件目录、surface、投影 |
| `packages/core/session/src/repair.ts` | 中断轮次的闭合 |
| `packages/experimental/inspector/src/index.ts` | 开发期观测入口 |

## 6. 最后

四季之后还值得写的东西不多，这两层是例外：它们不在产品表面，却决定了产品边界上的每一个「为什么不能」。

系列导航：[Cordis 内部](/posts/dsh-cordis-internals.html) · [三层组装](/posts/dsh-bundle-assembly.html) · [落盘](/posts/dsh-data-at-rest.html) · [现场诊断](/posts/dsh-diagnostics.html)
