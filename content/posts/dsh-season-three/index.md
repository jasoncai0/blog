---
title: "第三季：从工具到约束"
date: "2026-11-14T17:00:00+08:00"
summary: "第三季补三块：工具族的参数与失败模式、工具跑起来之后的运行时底座（遥测、存储、凭据、护栏、LSP/SSH），以及成本与安全两条横切线。"
description: "DSH 源码解读第三季导览：13 篇的目录、三条阅读路径、写作与证据约定。"
tags: ["harness", "architecture", "meta"]
slug: "dsh-season-three"
---

> 依据：DeepSeek Harness `0.2.2-rc.1`，commit `fcc0ad46`（工作区 `harness-research/deepseek-harness/`）。正文行号以该 commit 为准。第三季第 0 篇，导览。前两季：第一季《一个 Agent Harness 的解剖》21 篇，第二季《把主干两侧补齐》11 篇，均已发布。

前两季把一条请求讲完了：输入怎么排队、循环怎么转、工具怎么裁决、结果怎么回填、上下文怎么压缩。第二季又把外围接上：模型层、客户端、执行环境、MCP、Skill、交互面、宿主组装、嵌入协议。

还剩一层没讲：工具自己。31 个 `tool-*` 包，前两季只讲过它们共用的流水线，没讲过一个 `read` 为什么要 `offset`、`bash` 为什么有持久会话、`web_fetch` 为什么先问 provider 准备好没有。参数不是随手定的，每个参数背后都有一次真实的失败。

{{< fig "diagrams/01-three-bands.svg" "第三季的三块：工具族、运行时底座、全局约束。越往下越少被文档提及，也越容易在真实部署里出事。" >}}

## 0. 结论

第三季做三件事：

1. 工具族逐个拆（5 篇）：文件、Shell/终端、网络/浏览器、协作、PTC。每篇回答「这个工具的参数为什么长这样、失败长什么样」。
2. 运行时底座（5 篇）：遥测、存储、凭据、护栏、LSP/SSH。这些是工具跑起来之后才暴露的问题，也是 demo 与产品的分界。
3. 两条横切线与一条实践路径（3 篇）：成本与安全、从零写一个插件。

读法可以按需：查工具细节跳到第 2 节，查部署问题跳到第 3 节，要动手写插件直接看第 12 篇。

## 1. 前两季覆盖了什么

| 季 | 篇数 | 覆盖 | 没覆盖 |
| --- | --- | --- | --- |
| 第一季 | 21 | 输入、循环、工具流水线、子代理、记忆、压缩、召回、从零搭建 | 单个工具的参数语义、运行时底座、成本与安全 |
| 第二季 | 11 | 模型层、客户端、执行环境、MCP、Skill、交互面、宿主、嵌入协议、状态地图 | 同上，且工具只到「流水线」一层 |
| 第三季 | 13 | 工具族、遥测/存储/凭据/护栏/LSP/SSH、成本与安全、插件开发 | 产品运营、计费系统、多租户部署 |

第二季最后一篇《状态与扩展点地图》给了「加功能挂哪里」的判据，但没有回答「挂上去之后怎么观测、怎么存、怎么授权」。这是第三季要补的。

## 2. 工具族：五个族，五种失败

{{< fig "diagrams/02-tool-families.svg" "31 个 tool-* 包按失败方式归成五族；每族一篇，讲参数、截断、超时与拒绝。" >}}

| 篇 | 主题 | 回答的问题 |
| --- | --- | --- |
| 1 | `dsh-file-tools` | read 的 offset/limit、edit 的唯一匹配、陈旧读取与并发写 |
| 2 | `dsh-shell-and-terminal` | 一次性、持久、PTY 三套语义；输出环与 spill；何时转后台 |
| 3 | `dsh-web-and-browser` | 搜索/抓取的 provider 检查、正文提取、SSRF 边界、何时交给浏览器 |
| 4 | `dsh-collaboration-tools` | todo 为什么写事件、ask_user 怎么阻塞、present 与交付物 |
| 5 | `dsh-ptc-runtime` | 程序当工具跑：沙箱、子调用回流水线、省 token 的代价 |

五篇的共同结构：参数契约 → 失败分类 → 审批与沙箱插入点 → 自己搭时的取舍。

## 3. 运行时底座：工具跑起来之后

| 篇 | 主题 | 回答的问题 |
| --- | --- | --- |
| 6 | `dsh-telemetry` | span 结构、三层遥测的分界、token 记账与隐私 |
| 7 | `dsh-persistence-and-storage` | JSONL 与 SQLite 的取舍、写放大、迁移与损坏恢复 |
| 8 | `dsh-credentials-and-identity` | 四类凭据的解析顺序、刷新、脱敏、越权路径 |
| 9 | `dsh-guards-and-hooks` | guard 只能说不、hook 能观察改写、误报与漏报 |
| 10 | `dsh-lsp-and-ssh` | 诊断如何进上下文、远程执行替换哪个 seam、断线怎么办 |

这一块有一个共同点：它们都不改变模型看到的消息，只改变消息背后的世界。所以它们的 bug 不会立刻表现为「模型答错」，而是「模型答得对但落不了地」。

## 4. 两条横切线

第 11 篇《成本与安全》把前 10 篇的机制收束成两条线：

- 成本：一次请求的 token 从哪来、缓存怎么算、压缩的盈亏平衡点、并发与预算谁管。
- 安全：输入、模型、工具、沙箱、凭据五层边界，以及每层的绕过方式。

第 12 篇《从零写一个插件》是实践：从 `package.json` 的 `dsh.bundle` 声明，到 Cordis 的 `ctx.effect()` 注册，再到本地 profile 装载、REAL-composition 测试与分发。

## 5. 怎么读

| 你的问题 | 路径 |
| --- | --- |
| 「这个工具为什么这么设计」 | 2 → 3 → 4 → 5，按工具族读 |
| 「上线后为什么出问题」 | 6 → 7 → 8 → 9 → 10，按底座读 |
| 「我要做一个类似的 harness」 | 11 → 12，先看成本与安全，再动手写插件 |

三条路径都不要求按编号读；每篇开头都有「这篇回答什么」，结尾有前后篇链接。

## 6. 写作与证据约定

- 行号以 `fcc0ad46` 为准；只写文件名的条目表示没有逐行核对。
- 源码引用优先于文档；文档与源码冲突时以源码为准，并在文中标出。
- 图由源码结构生成，不画「想象中的架构」；图内文字与正文结论一致。
- 数字（上限、默认值、超时）都来自源码或 `docs/`，不估算。

## 7. 源码索引

| 主题 | 文件 |
| --- | --- |
| 工具目录 | `docs/tool-catalog.zh.md`（2706 行） |
| 文件系统 | `docs/subsystems/filesystem.zh.md`、`packages/fs/` |
| Shell / 终端 | `docs/subsystems/shell.zh.md`、`terminal.zh.md`、`packages/shell/`、`packages/terminal/` |
| 网络 | `docs/subsystems/web.zh.md`、`packages/web/` |
| PTC | `docs/subsystems/ptc-runtime.zh.md`、`packages/ptc-runtime/` |
| 遥测 | `docs/subsystems/otel.zh.md`、`session-telemetry.zh.md`、`token-meter.zh.md` |
| 存储 | `docs/subsystems/storage.zh.md`、`persistence.zh.md`、`packages/storage/` |
| 凭据 | `docs/subsystems/credentials.zh.md`、`packages/credentials/` |
| 护栏 | `packages/guard/`、`docs/subsystems/permission-presets.zh.md` |
| LSP / SSH | `docs/subsystems/lsp.zh.md`、`ssh.zh.md`、`packages/lsp/`、`packages/ssh/` |
| 插件 | `docs/development.zh.md`、`docs/subsystems/extensions.zh.md`、`packages/AGENTS.md` |

## 8. 系列导航

- 上一篇：无（第三季开篇）
- 下一篇：[文件工具族](/posts/dsh-file-tools.html)
- 第一季入口：[解剖](/posts/dsh-architecture-anatomy.html)；第二季入口：[把主干两侧补齐](/posts/dsh-season-two.html)

## 最后

第三季的 13 篇仍然遵循同一条线索：每个设计都对应一次具体的失败。读到某个参数觉得多余时，先找它防的那次失败；找到之后，参数通常就只剩一种写法。
