---
title: "OpenDesign 上下文面：prompt 编译器与四类内容平面"
date: 2026-11-19T19:01:00+08:00
summary: "system prompt 的十三组区块怎么拼、两套实现为什么会漂移，以及 skills / templates / design-systems / plugins 四类目录的遮蔽与消费规则。含 prompt 栈与内容平面图。"
tags: ["architecture", "agent"]
slug: "open-design-context"
series: "open-design"
series_order: 2
---


> 源码版本：`nexu-io/open-design` `0725880`（package.json 0.27.2）。本地 clone 后逐文件核对，行号都对这个 commit。仓库 14,308 个文件，`apps/daemon/src/server.ts` 一个文件 18,959 行。

本系列 9 篇 · [总览](/posts/open-design-architecture.html) · 上一篇 [执行面](/posts/open-design-runtime.html) · 下一篇 [产物面](/posts/open-design-deliverables.html)

## 3.4 prompt 编译器

`composeSystemPrompt`（`prompts/system.ts:899`）的函数体第一行就是分叉：

```ts
if (odNextStrategyRecipe) return composeOdNextStrategyRequestPromptV2(...)
```

没有 recipe 就走 slim core charter（`core-slim.ts:425`，默认；`OD_PROMPT_CORE=classic` 切回）。正文有三十余个区块，按职责能合并成下面十三组。

{{< fig "diagrams/05-prompt-stack.svg" "prompt 的组装顺序与两道闸门。左列是顺序，右侧三块是分叉点、已发生的漂移和明确不做的三件事。" >}}

顺序里有三处判断值得单独拎出来。有设计系统时，"设计方向"整段被跳过（`:1150`），因为 `DESIGN.md` 已经回答了风格问题。craft 规则夹在品牌 token 块与 skill 正文之间（`:1331`），冲突时品牌 token 优先。角色边界守卫放在最后一段（`:1530`），用来压住前面所有指令，包括用户自己写的。

体积管理只有一个动作：组装完按字节卡上限。超限直接报 `AGENT_PROMPT_TOO_LARGE`，不做裁剪、不做分节丢弃。"按 DESIGN.md 分节裁剪"这个字段（`od.design_system.sections`）在协议里预留了，实现没有。

风险出在"两套实现"上。strategy 维度有 legacy 与 OD Next 两条路，core 维度有 slim 与 classic 两个头，加上 `apps/daemon/src/prompts/` 与 `packages/contracts/src/prompts/` 之间五对手抄镜像，漂移已经发生：`system.ts` 一边 2048 行、一边 1141 行，`core-slim.ts` 在 contracts 侧根本不存在。

#7568 是这套结构的必然产物：deck 协议只加在 legacy 那一侧，两个新增守护都调用 `composeSystemPrompt` 且不带 `odNextStrategyRecipe`，所以它们永远走 legacy 分支、永远绿。#7651 的修法是把协议收口到单源 `packages/contracts/src/prompts/deck-framework.ts:573`。`docs/prompt-composition.md` 留了一句话，我认为是这份代码库里最该贴在墙上的一句：绿色测试不证明两边都带上了你的改动。

## 3.5 内容平面：四类目录加一套通用规则

{{< fig "diagrams/06-content-planes.svg" "四类内容平面、遮蔽规则，以及同一条 skill 记录的三处消费：prompt、工作目录、UI 分组。" >}}

规模：`skills/` 171 个 `SKILL.md`，`design-templates/` 116 个，`design-systems/` 154 个包，`plugins/_official/` 6 个工作流，`craft/` 13 条品牌无关规则。

发现逻辑在 `skills.ts` 与 `server.ts:1494` 的 `SKILL_ROOTS`。用户目录先扫，仓库目录后扫，`frontmatter.name` 相同就停（`skills.ts:284` 那行 `if (seenIds.has(parentId)) continue`）。每次列表请求重新扫盘，没有 watcher、没有 SIGHUP、没有安装步骤：把文件夹丢进去，下一次刷新就能看见。

一个 skill 出现在三个地方，这三处必须一起看：

1. **prompt 里**：选中 skill 的 `SKILL.md` 正文，不带 `references/*.md`，正文里指路让 agent 自己去读。`@` 提及的 skill 与插件带的 skill 一起拼进来。
2. **工作目录里**：每轮把 skill 目录拷到 `<cwd>/.od-skills/<basename>-<sha256[:10]>/`（`cwd-aliases.ts:108`）。用真实拷贝。注释写了原因：早期用 symlink 指向 `skills/`，agent 往工作目录一写，就把仓库里的发布资源改了（`:9-29`）。
3. **UI 里**：`od.mode`、`od.surface`、`od.category` 决定新建面板的分组。缺 `od:` 块时按 `prototype` 兜底，所以 171 个 skill 里那些老文件不需要改就能被识别。

设计系统包的结构是 `manifest.json` + `DESIGN.md` + `tokens.css`，可选 `USAGE.md`、`components.manifest.json`、`components.html`、`design-tokens.json`、`preview/`、`source/`。校验很硬：`schemaVersion` 必须等于 `od-design-system-project/v1`，`id` 必须等于目录名，`files.design` 必须指向 `DESIGN.md`（`design-systems/index.ts:4114-4162`）。token 契约（`token-contract.ts:171-199`）把未声明的 `var()` 判成错误，把一屏内出现超过两次的 `var(--accent)` 判成警告。

插件是"带流水线的 skill 包"：目录 + `SKILL.md` + `open-design.json`，后者声明 `od.pipeline.stages[].atoms[]`。安装器（`plugins/installer.ts:1-20`）支持本地目录、`github:owner/repo`、`.tar.gz`，拒路径穿越、符号链接、超过 50MiB、覆盖已有 id。信任分 trusted 与 restricted 两级，能力声明决定它能拿到哪些工具。运行时把 atom 展开成有序阶段，`pipeline-runner.ts:55` 逐阶段发 `pipeline_stage_started` 和 `completed`，`until` 词表是封闭集合（`critique.score`、`iterations`、`user.confirmed`、`preview.ok`、`build.passing`、`tests.passing`），devloop 轮数上限默认 10。

---

下一篇是[产物面](/posts/open-design-deliverables.html)。回到[总览](/posts/open-design-architecture.html)，或者从[分册索引](/posts/open-design-architecture.html#分册索引)挑一个方向。
