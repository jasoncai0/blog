---
title: "从能跑到跑对：搭一个本地 Coding Agent 踩过的坑"
date: "2026-11-15T16:40:00+08:00"
summary: "一个本地 Coding Agent 从 demo 做到可交付，真正耗时的不是 agent loop，而是验证边界、执行环境、权限审批和失败归因。这是系列总览，按循环、执行环境、会话与历史拆分。"
description: "Jade 项目的故障复盘系列总览：十阶段循环、工具/技能发现、四语言验证、macOS 沙箱、权限审批、会话恢复与连续追问；包含轮次/预算取舍、SDK 下载污染、Maven/Spring Boot 沙箱失败等具体错误与修法。"
tags: ["harness", "codegen", "architecture"]
slug: "coding-agent-pitfalls"
---

> 项目：[`jasoncai0/Jade`](https://github.com/jasoncai0/Jade)（公开仓库），Jade 1.0.0，commit `4245fb6`。
> 环境：macOS 15.6，JDK 23，Maven 3.9.9，Python 3.13，Node 23，Go 1.27。
> 这不是 API 教程，是一份故障复盘。原文约 31 KB，已拆成三篇专题；项目内 `docs/BLOG_MEMORY.md` 会继续追加新坑，系列也会同步更新。

## 0. 结论

这个项目最后能做的事不复杂：本地起一个 Web 控制台，把任务交给 DeepSeek，模型通过工具改工作区，运行时用真实命令编译、运行、验证，最后把过程通过 SSE 推给浏览器。

真正花时间的，是下面这些判断：

1. **`MAX_STEPS` 不是错误原因。** 它只说明循环在显式配置的轮次停了；工件可能已经跑通，也可能根本没编译过。默认已改为不限轮数，用无进展反思、重复调用和命令形状护栏防止失控；token 预算只是可选的成本上限。
2. **源码发现就是安全边界。** 把 Go SDK 下载进工作区后，验证器会把 SDK 里的 JavaScript 当成项目源码。
3. **写沙箱不是容器。** Maven 的共享缓存、JVM 的临时目录、下载网络、本地端口，各自都是独立假设。
4. **编译通过不等于运行通过，运行通过不等于测试通过。** 验证器只证明它实际执行过的那一步。
5. **工具和技能的“发现”必须像代码一样被测。** ServiceLoader 的 provider 名字写错，生产不会报错，只会少一个工具。
6. **会话恢复不是把 JSON 读回来。** 事件序号、终止事件、进行中状态和日志生命周期都要一起恢复；`run.finished` 只结束一轮，不结束会话。
7. **Trace 很有用，也很敏感。** 请求体里有完整任务、上下文和工具输出，不该和 API key 一起被记录。


## 1. 长什么样

一次任务启动后，`AgentService` 建一个独立工作区，`AgentLoop` 每个 turn 固定走十个阶段：

`INTAKE → CONTEXT → SKILLS → PLAN → MODEL → TOOLS → OBSERVE → VERIFY → COMPACT → CHECKPOINT`

模型只负责提案：调用哪个工具、写什么文件、什么时候停。文件真的落盘，`javac` / `python3` / `node` / `go` 真的执行，输出再作为 tool message 回灌给模型。默认不限制 turn；连续 8 个工具轮次没有进展时注入反思检查点，16 轮停止。

{{< fig "diagrams/01-architecture.svg" "Jade 的运行结构：Web/CLI 只观察；AgentService 管 run 生命周期；AgentLoop 管十阶段循环；工具和技能注册表提供能力；工作区与验证器负责把“说了”变成“做了”。" >}}

这个结构有两个刻意的选择。

第一，**模型不接触宿主机**。它只能拿到 `Workspace` 包装过的路径；`read_file`、`write_file`、`update_plan` 都走同一层。第二，**验证不靠模型自述**。`compile_code` / `run_code` 执行的是真实进程，验证结果写成事件，也写进 `RunSnapshot`。


## 2. 系列拆分

原文太长，按“失败发生在哪一层”拆成三篇。总览保留结论、系统结构、检查清单和设计判断；具体排查过程放到专题里。

1. [循环护栏：从 `MAX_STEPS` 到反思机制](/posts/coding-agent-loop.html) — 为什么轮次会掩盖两种失败，8/16 轮反思、同参数 4 连、命令形状 8/10 怎么配合，验证器为什么只证明编译。
2. [执行环境：SDK 污染、沙箱边界与多语言验证](/posts/coding-agent-sandbox.html) — `.toolchain` 被当项目源码、写沙箱挡不住 Maven/Spring Boot、`node --check` 和语言投票的细节。
3. [会话与历史：连续追问、权限、MySQL 分页与压缩](/posts/coding-agent-sessions.html) — `run.finished` 的生命周期、审批卡、长会话 96 MB 事件、MySQL 分页、token 预算与模型摘要。

## 3. 一份检查清单

| 现象 | 先看 | 不要做 |
|---|---|---|
| `MAX_STEPS` | verification、events、工作区 | 直接判定代码失败 |
| 反复读同一文件 | `reflection.requested`、`reflection.recorded`、命令形状 | 只把轮数调大 |
| CLI 跑完不退出 | `run.finished`、终端快照、`RunEventLog.awaitAfter` | 直接 kill，误判成模型卡住 |
| 语言选错 | SDK/依赖目录、扩展名统计 | 只看文件数量 |
| Maven 失败 | `~/.m2` 权限、网络、tmp | 只加 turn 上限 |
| 启动失败 | 端口、临时目录、bind 权限 | 只看编译日志 |
| 工具缺失 | ServiceLoader 服务文件、重名 | 只测内置工具 |
| 技能没生效 | frontmatter、排序、注入 | 只检查文件存在 |
| `SUCCEEDED` 但行为不对 | 是否真的 run/test | 相信模型自述 |
| 重启后 run 消失 | `run.finished`、恢复顺序 | 只恢复内存 Map |
| 会话不能追问 | 日志/SSE 是否在每轮关闭 | 每轮另起一个 run |


## 4. 最后的复盘：六个设计判断

把整个项目压缩，最后留下来的是六个判断，而不是六段代码。

1. **模型负责提案，运行时负责不变量。** 模型决定写什么、跑什么；验证门、权限门、路径限制、事件记录必须由运行时强制执行。否则“模型说完成了”就会变成系统结论。
2. **默认值就是产品行为。** 沙箱默认关、轮次默认不限、预算默认不限、权限默认 balanced。默认开一个会挡住本地服务的写监狱，或者默认用一个会截断长任务的总预算，都是在替用户做错误决定。
3. **权限不是沙箱。** 审批卡解决“用户是否授权”，不解决“代码是否安全”。用户批准后的 `python -c` 仍能访问主机；真正的隔离要靠容器/VM/OS 策略，文档必须把这句话写清楚。
4. **`run.finished` 不是会话终点。** 它只表示这一轮模型结束了。连续追问复用同一个 workspace、transcript 和事件日志，SSE 也不该在一轮结束时断开。
5. **轮次和 token 预算是代理指标。** 轮次不衡量进展，token 不衡量价值。正常完成、连续相同工具调用、用户取消、显式成本上限，才是更可靠的停止信号。
6. **状态、日志、工件分开看。** 终态可能是 `MAX_STEPS` 但工件可用，也可能是 `SUCCEEDED` 但运行没跑过。先看 verification，再看 events，最后进工作区跑构建/测试。


## 5. 后续

这篇不是一次性总结。项目里新增了 `docs/BLOG_MEMORY.md` 和项目级 `AGENTS.md`：以后在这个项目里遇到新的坑，先把“症状 → 根因 → 修法 → 对应章节”记进项目记忆，再更新这篇文章并重新发布。记忆只留在项目里，不写全局配置。

如果这篇能留下一个最有用的习惯，那就是：**状态、日志、工件，三者分开看。** Agent 说“完成了”不算证据，编译器、运行时和测试进程说完成，才算。

