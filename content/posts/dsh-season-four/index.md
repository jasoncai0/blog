---
title: "第四季：看那些不写在架构图上的机制"
date: "2026-11-16T20:00:00+08:00"
summary: "前三季按模块铺开，第四季转向横切机制：消息怎么投递、格式怎么迁移、错误在哪层被规范化、谁串行谁并发、实验面与 SDK 怎么接。十篇，附三张图。"
description: "DSH 深挖第四季导览：十篇机制层文章的范围、依赖关系与阅读路径，以及这一季的证据约定。"
tags: ["harness","architecture","meta"]
slug: "dsh-season-four"
---

> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46）。本季文章的行号都以该 commit 的工作树为准；凡未做运行时验证的结论，会在正文里标出推断依据。

前三季把 DSH 拆成模块：循环、输入、工具、委派、记忆、客户端、执行环境、宿主组装。每一篇回答「这个东西怎么工作」。

还有一类问题不在任何单个模块里，却决定模块之间的行为：子代理结果以什么身份进入父 Agent 的上下文；会话格式升级时旧日志还能不能读；工具失败在哪个边界变成 `isError`；`send_message` 和 `inject` 同时到达时谁先被领走。

第四季就写这些。{{< fig "diagrams/01-map.svg" "第四季的十篇分成四簇：消息与兼容、错误与并发、实验面与接入、读法。" >}}

## 1. 前三季留下了什么

| 季 | 篇数 | 覆盖 | 留下的缺口 |
| --- | --- | --- | --- |
| 第一季 | 21 | 请求生命周期：输入、循环、工具、委派、记忆、压缩 | 只讲单条主路径 |
| 第二季 | 11 | 主干两侧：模型层、客户端、执行环境、MCP、Skill、宿主、协议 | 模块内部细节 |
| 第三季 | 13 | 工具族与运行时底座：文件、Shell、Web、协作、PTC、遥测、存储、凭证、护栏、LSP/SSH | 模块之间 |

第四季的十篇都落在最后一行：不新增模块，只把模块之间的约定写清楚。

## 2. 第四季写什么

| # | 篇目 | 范围 |
| --- | --- | --- |
| 1 | [Agent 之间怎么说话](/posts/dsh-agent-messaging.html) | `send_message`、`followup`、`steer`、`inject` 与结算通知的投递矩阵 |
| 2 | [会话格式怎么演进](/posts/dsh-session-compat.html) | v0→v4 代际、迁移链、严格回放、resume/fork 切点 |
| 3 | [错误地图](/posts/dsh-error-atlas.html) | 各层错误码、可重试性、模型看到什么 |
| 4 | [怎么测一个 harness](/posts/dsh-testing-strategy.html) | REAL 组合测试、假模型、按事件断言 |
| 5 | [谁串行，谁并发](/posts/dsh-concurrency-and-ordering.html) | 锁、CAS、批次顺序、取消竞态 |
| 6 | [实验面](/posts/dsh-experimental-surface.html) | computer-use、browser-use、action cards、workspace、deliverables |
| 7 | [嵌入与网关](/posts/dsh-sdk-and-gateway.html) | SDK 生命周期、WebSocket 协议、重连与背压 |
| 8 | [Agent preset](/posts/dsh-agent-presets.html) | 一个 profile 里的多种 agent，继承与覆盖 |
| 9 | [从帧到界面](/posts/dsh-ui-streaming.html) | 流式渲染、窗口、重连、批处理 |
| 10 | [怎么读这份代码库](/posts/dsh-reading-dsh.html) | 模块图、codegraph、证据等级、常见误读 |

排序大致按依赖：先消息与格式（数据怎么流动），再错误与并发（失败与顺序），然后实验面与接入（边界之外），最后是读法。

## 3. 三条阅读路径

{{< fig "diagrams/02-paths.svg" "三条路径：排障、做插件、做嵌入，各自的起点与收口不同。" >}}

| 路径 | 适合 | 顺序 |
| --- | --- | --- |
| 排障 | 线上行为不对，想知道去哪看 | 错误地图 → 并发与顺序 → 会话兼容 → 消息 |
| 做插件 | 想加工具、加 agent 能力 | 实验面 → preset → 消息 → 测试 |
| 做嵌入 | 把 DSH 接进自己的产品 | SDK 与网关 → 会话兼容 → 客户端流式 → 并发 |

三条路径都不要求按编号读。每篇开头列出要回答的问题，结尾有上下篇链接。

## 4. 这一季的证据约定

和前三季一致，分四级：

| 等级 | 含义 | 写法 |
| --- | --- | --- |
| A | 逐行读过实现，行为可对照源码 | 直接陈述，给 `path:line` |
| B | 只读类型与接口，未跟到实现 | 句子里写「按类型定义」 |
| C | 只读文档或 README | 句子里写「文档称」 |
| D | 从结构推断，无直接证据 | 写成假设，并给出验证方法 |

错误码、事件名、字段名一律用源码原文；中文只做解释，不替换标识符。

## 5. 设计取舍

写机制层比写模块层难，取舍有三条：

| 取舍 | 选择 | 代价 |
| --- | --- | --- |
| 覆盖 vs 深度 | 每篇只追一条链路，追到底 | 同一模块可能在不同篇出现两次 |
| 源码 vs 运行 | 以源码为主，运行验证为辅 | 涉及时钟、网络、并发的结论只能标推断 |
| 稳定 vs 时效 | 只写 0.2.2-rc.1 的行为 | 版本升级后行号会漂，标识符相对稳定 |

## 6. 踩坑点

1. 把「模块」当「能力边界」。包名只说明归属，能力边界看 seam 与 scope。
2. 只看 README 就下结论。README 描述模型体验，不保证实现细节。
3. 用行号当唯一锚点。行号会漂，函数名与事件名更稳。
4. 把测试当规范。测试覆盖的是作者想到的路径，不是全部契约。
5. 忽略 `ignorable` 与版本字段，读到旧日志时直接跳过未知事件。

## 7. 源码索引

| 位置 | 内容 |
| --- | --- |
| `docs/architecture.zh.md` | 组合、事件域、轮次流程、扩展点总表 |
| `docs/module-graph.zh.md` | 370 包的依赖图 |
| `docs/tool-catalog.zh.md` | 31 个工具包的参数与行为 |
| `docs/subsystems/` | 69 篇子系统契约 |
| `docs/glossary.zh.md` | seam、scope、turn/step、goal 等术语 |
| `packages/AGENTS.md` | 包级实现约定 |

## 8. 最后

前三季解决「这个模块是什么」，第四季解决「模块之间靠什么约定拼在一起」。读完这一季，再回头看第一季的时序图，很多当时略过的箭头会有名字。

系列导航：[第三季导览](/posts/dsh-season-three.html) · [整体解剖](/posts/dsh-architecture-anatomy.html) · [状态与扩展点地图](/posts/dsh-state-and-extension-atlas.html)
