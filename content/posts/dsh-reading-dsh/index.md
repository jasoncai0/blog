---
title: "怎么读这份代码库：370 个包、69 篇文档与一条证据链"
date: "2026-11-16T22:30:00+08:00"
summary: "DSH 有 370 个包、46 万行 TypeScript。按目录硬读容易迷路。从物理结构、六种包角色、四类文档的权威范围、codegraph 的用法，到「从事件名追到测试」的五步法，给出一套可复核的读法。"
description: "DSH 代码库阅读指南：group/package 结构、service/provider/tool 的角色判据、architecture 与 subsystems 文档的分工、codegraph 查询、证据等级与常见误读。"
tags: ["meta","harness","architecture"]
slug: "dsh-reading-dsh"
---

> 依据：DeepSeek Harness 0.2.2-rc.1（commit fcc0ad46）的工作树。规模数字由 `find` 与 `grep` 实测；包角色判据来自 `packages/AGENTS.md` 与 `docs/architecture.zh.md`。

打开 DSH 的仓库，第一眼是 53 个 group、370 个包、46 万行 TypeScript。按目录从上往下读，读到第三个 group 就会忘记第一个讲了什么。

下面按四件事展开：文件放在哪、文档谁说了算、一个行为从哪里追进去、什么算证据。

{{< fig "diagrams/01-repo-map.svg" "物理结构：group 下是包，包下是 src、tests、README；跨包的契约在 docs/subsystems。" >}}

## 1. 物理结构

```text
deepseek-harness/
  packages/<group>/<package>/
    package.json          # 名称、exports、dsh.bundle / dsh.profile
    src/*.ts              # 实现
    tests/*.spec.ts       # 单测
    tests/*.e2e.ts        # 组合测试
    README.md             # 模型体验与已知限制
    README.zh.md
  docs/
    architecture.zh.md    # 组合、事件域、扩展点
    module-graph.zh.md    # 依赖图
    tool-catalog.zh.md    # 31 个工具包
    subsystems/*.zh.md    # 69 篇子系统契约
    glossary.zh.md        # 术语
```

包名不等于能力边界。`packages/fs/fs-local` 提供本地文件系统，`packages/fs/tool-fs` 把能力包成工具；两者都在 `fs` 组，但一个是 provider，一个是 consumer。

## 2. 六种包角色

判断一个包是什么，看三处：`package.json` 的 exports、`src/index.ts` 的导出、以及它有没有 `ctx.<key>`。

| 角色 | 判据 | 例子 |
| --- | --- | --- |
| Service Definition | 默认导出 class，带 `ctx.<key>` 注册 | `core/session`、`core/tools` |
| Service Provider | 实现上面的 class，通常在 config 里替换 | `fs/fs-local`、`shell/bash-local` |
| Consumer | `inject` 声明依赖，`apply(ctx)` 里用服务 | `tool/tool-fs`、`compaction/compaction-basic` |
| Tool | 注册 `ToolDefinition`，有 schema 与 execute | `tool/tool-bash` |
| Function plugin | 命名导出 `name/inject/Config/apply`，无 default | `context/time-context` |
| Library | 不碰 `ctx`，纯函数与类型 | `core/scope` 的部分模块 |

`packages/AGENTS.md` 给了一条硬规则：function plugin 不能有 default 导出，service 包必须有；混用会被 Loader 丢弃。这条规则本身就是判据。

{{< fig "diagrams/02-evidence-ladder.svg" "四级证据：运行行为、源码实现、类型定义、文档描述。越往下越容易读，也越容易过时。" >}}

## 3. 四类文档，各管一段

| 文档 | 回答什么 | 不该拿它回答 |
| --- | --- | --- |
| `architecture.zh.md` | 组合顺序、事件域、轮次流程、扩展点归属 | 某个函数的边界条件 |
| `subsystems/*.zh.md` | 子系统的契约：类型、状态机、失败语义 | 跨子系统的调用顺序 |
| `tool-catalog.zh.md` | 每个工具的 schema、参数、返回 | 工具内部实现 |
| `module-graph.zh.md` | 包之间的依赖方向 | 运行时行为 |

文档里的 `ts type-equiv` 代码块是被校验的类型原文，可以直接引用；普通代码块是示例，可能简化过。

## 4. codegraph：先查图，再 grep

仓库根有 `.codegraph/codegraph.db`。它把符号、调用边、文件建成索引，适合回答「谁调用谁」「这个类型在哪实现」。

```bash
codegraph explore "ToolBatchExecutionError partial results"
```

一次调用返回相关符号的逐行源码与调用路径。适合起点模糊的问题；如果你已经知道精确字符串（事件名、错误码），grep 更快。

两者不冲突：grep 找字面量，codegraph 找关系。

## 5. 追一条行为的五步法

以「工具结果为什么带 `sourceEventSeqs`」为例：

1. 找事件名。`grep -rn "tool/result" packages/core/session/src/types.ts`，确认它在 `SessionEventMap` 里。
2. 找写入点。`grep -rn "append('tool/result'" packages`，落到 `core/tools/src/pipeline.ts`。
3. 找投影。`grep -rn "deriveEventMessage" packages/core/session/src`，看它如何变成 wire 消息。
4. 找消费方。`grep -rn "sourceEventSeqs" packages --include=*.ts`，看谁读它（压缩、UI、审计）。
5. 找测试。`grep -rn "sourceEventSeqs" packages/**/tests`，看作者断言了什么。

五步走完，一个字段的生命周期就闭合了。缺任何一步，结论都只能算推断。

{{< fig "diagrams/03-trace-loop.svg" "五步法：事件名 → 写入点 → 投影 → 消费方 → 测试；每一步都要落到文件与行号。" >}}

## 6. 证据等级

| 等级 | 证据 | 写法 |
| --- | --- | --- |
| A | 逐行读过实现，或跑过运行时 | 直接陈述，给 `path:line` |
| B | 只读类型与接口 | 「按类型定义」 |
| C | 只读文档或 README | 「文档称」 |
| D | 从结构推断 | 写成假设，给验证方法 |

写文章、做评审、回答「为什么这样设计」时，先标明等级。等级混用是这类代码库最常见的错误来源：README 说「自动」，实现里可能只在某个条件下自动。

## 7. 常见误读

1. 把 group 当模块。`subagent` 组里有工具、有 manager、有 ACP 适配，角色各不相同。
2. 把 README 当规范。README 的 Model Experience 描述模型看到什么，不保证边界条件。
3. 看到 `ctx.xxx` 就以为是一个服务。有些是投影句柄，有些是只读视图，生命周期不同。
4. 用测试反推契约。测试覆盖作者想到的路径，没覆盖的不代表允许。
5. 忽略 `ignorable`。旧读器遇到未知事件是否拒绝，取决于这个字段，不看版本号。
6. 把 `packages/experimental` 当稳定 API。实验包可以没有兼容承诺。
7. 只看 `src/index.ts`。多数行为在 `pipeline.ts`、`manager.ts`、`fold.ts` 这类文件里。
8. 用行号当锚点。升级后行号会漂，函数名与事件名更稳。

## 8. 三条阅读路径

| 目标 | 顺序 |
| --- | --- |
| 想改一个工具 | `tool-catalog` → `tool-*` 包 → `core/tools` 流水线 → 对应测试 |
| 想加一个事件 | `SessionEventMap` → 写入点 → 投影 → 消费方 → 回放校验 |
| 想换一个 provider | `architecture` 的 seam 表 → Definition 包 → 现有 provider → REAL 组合测试 |

## 9. 设计取舍

| 取舍 | 选择 | 代价 |
| --- | --- | --- |
| 包多 vs 包大 | 一个能力一个包，seam 三角色分开 | 找入口要先看 group |
| 文档多 vs 文档少 | 每个子系统一篇契约 | 文档与代码可能不同步 |
| 类型强 vs 上手快 | branded number、事件联合类型 | 读代码要同时读类型 |
| 证据严 vs 写得快 | 结论标等级 | 写作速度慢 |

## 10. 踩坑点

1. 直接读 `src/index.ts`，错过实现文件。
2. 把 `dsh.bundle` 当运行时插件清单，它只是分发声明。
3. 用 `package.json` 的 name 猜能力，忽略 `exports` 与 `ctx` 注入。
4. 在 docs 里找行号，文档只保证类型与契约。
5. 把 codegraph 当全文搜索，它按符号建索引，字面量仍要 grep。
6. 忽略 `tests/fixtures`，那里常有最小可运行组合。
7. 把 `Model Experience` 当营销文案，它记录的是模型实际看到的提示词与工具描述。

## 11. 源码索引

| 位置 | 内容 |
| --- | --- |
| `docs/architecture.zh.md` | 组合、事件域、扩展点归属 |
| `docs/glossary.zh.md` | seam、scope、turn/step、goal |
| `docs/module-graph.zh.md` | 包依赖图 |
| `docs/tool-catalog.zh.md` | 工具 schema 与行为 |
| `packages/AGENTS.md` | 包角色与实现约定 |
| `packages/core/session/src/types.ts` | `SessionEventMap` 与事件形状 |
| `packages/core/tools/src/pipeline.ts` | 工具流水线实现 |
| `packages/subagent/subagent/src/manager.ts` | 子代理生命周期与投递 |
| `.codegraph/codegraph.db` | 符号与调用边索引 |

## 12. 最后

读这份代码库的顺序，和它的组织方式相反：先从事件与类型入手，再回到包；先确认一个字段的生命周期，再看它属于哪个 group。

一个可执行的检验：随便挑一个事件名，按五步法走一遍。走得通，说明你已经会读这个仓库了。

系列导航：[第四季导览](/posts/dsh-season-four.html) · [整体解剖](/posts/dsh-architecture-anatomy.html) · [状态与扩展点地图](/posts/dsh-state-and-extension-atlas.html)
