---
title: "组合与装配：profile、bundle、patch 三层"
date: "2026-11-17T18:40:00+08:00"
summary: "DSH 的启动配置不是一份 JSON，而是五层 patch 依次盖在空列表上。行 id 是唯一寻址方式，一次 patch 替换整条 config，preset 再往里钻一层。"
description: "profile/bundle/patch 的装配语义：五层叠加、按 id 替换、disabled 谓词、preset 嵌套、HMR 边界。"
tags: ["architecture","codegen","harness"]
slug: "dsh-bundle-assembly"
---


> 依据：DSH 0.2.2-rc.1（commit fcc0ad46）。`packages/bundle/base/cordis.patch.yml` 的注释、`docs/architecture.zh.md` 的组合章节、`packages/boot/app-boot/README.zh.md`。未在运行时抓取完整的 `--dump-config` 输出。

一个产品要支持五种运行形态（Web、无头、SDK、SDK 极简、ACP），同时允许用户加插件、改配置、换模型。多数项目的做法是：一份默认配置 + 若干份模式覆盖 + 代码里写 `if (mode === 'web')`。

DSH 的做法是另一个方向：配置本身就是数据，装配是五层数据的叠加，装配器里没有模式分支。

{{< fig "diagrams/01-layer-stack.svg" "五层叠加：空列表是起点，profile 的组合包、bundle 的 patch、home patch、命令行 overlay 依次生效。" >}}

## 1. 五层，从空开始

装配的输入是一条空条目列表，然后依次应用：

| 层 | 来源 | 谁写的 |
| --- | --- | --- |
| 1 | profile 中有序的组合包 | DSH 随附（`dsh.profile` 声明） |
| 2 | 每个组合包的 `cordis.patch.yml` | 组合包作者 |
| 3 | home patch（`$DSH_HOME` 下的 profile 配置） | 用户 |
| 4 | `--patch` overlay | 命令行调用方 |
| 5 | preset 内的 patch | 预设作者 |

后一层按行 id 改写，不做整层覆盖：一条 patch 要么替换某个 id 的整条 config，要么插入新行。

`packages/bundle/base/cordis.patch.yml` 的注释把语义写得很直白：

```yaml
# A patch replaces the targeted row's whole `config` rather than merging into
# it, so a row whose value differs by mode does NOT live here: it belongs to
# each mode bundle, keeping any single row down to one bundle layer plus the
# user's.
```

三句话，三个约束：

1. 替换而非合并：改一个字段要把整条 config 写全。这消除了「深合并时数组是替换还是拼接」的歧义。
2. 模式相关的行不进共享层：`dsh-base` 只放各模式一致的默认值；差异行由各模式 bundle 重新写全。
3. 每行最多两层：一个 bundle 层 + 用户层。这限制了「这个值到底从哪来」的追溯深度。

第 3 条是这套设计最重要的约束。没有它，五层叠加会变成「五个人都能改同一个字段」，出问题时没人能说清生效值来自哪一层。

## 2. 行的形状

```yaml
- insert:
    - id: llm
      name: '@deepseek-ai/dsh-llm'

    - id: hmr
      name: '@deepseek-ai/dsh-hmr'
      disabled: !!js "!ctx.get('profileContext')"
      config:
        root: []
```

四个可选字段：`id`（寻址键）、`name`（插件包名，可带子路径导出）、`disabled`（布尔或表达式）、`config`（原样传给插件）。

`!!js` 是 YAML 标签，值在装配时用 `ctx` 求值。上面这条的意思是「没有 `profileContext` 服务时禁用」，同一个 bundle 在 CLI 与测试宿主里行为不同，靠的是谓词而不是两份文件。

行顺序不承载加载语义。 注释原文：`Row order carries no load semantics (activation is service-availability driven)`。插件什么时候激活由 Cordis 的依赖满足决定，不由它在 YAML 里的位置决定。这条和 Cordis 那篇的 epoch 机制是同一个设计的两面。

## 3. 五种 profile

| profile | 组合包 | 用途 |
| --- | --- | --- |
| `web` | base + web-app | 浏览器应用 + 服务器 |
| `headless` | base + headless | 一次性运行，不起服务器 |
| `sdk` | base + sdk-app | JSON-RPC 服务器，给 SDK 调用 |
| `acp` | base + acp-app | 仅自动化的 ACP 服务器 |
| `sdk-minimal` | 自己一套完整配置树 | 刻意不套 base |

前四个共享 `dsh-base`：模型适配器、工具、持久化、沙箱与审批策略、设置、凭据、遥测。差异只在「应用外壳」这一层，有没有 HTTP 服务器、有没有浏览器客户端、有没有 RPC 入口。

`sdk-minimal` 是故意的例外：它不应用 `dsh-base`，自己写全部显式配置。理由是它要演示「最小可用组合长什么样」，如果套 base，读者看到的仍是一百多行默认值。

这个例外值得单独说：它证明装配器里没有「必须经过 base」的硬编码。base 不是内核，只是一个被四个 profile 共同引用的组合包。

{{< fig "diagrams/03-profile-map.svg" "五种 profile 的组合关系：四个共享 base，一个刻意独立。" >}}

## 4. patch 的三种操作

{{< fig "diagrams/02-patch-ops.svg" "三种操作：按 id 替换整条 config、插入新行、用谓词禁用。" >}}

### 替换

```yaml
- id: hmr
  config:
    root: ['src']
```

只写 `id` 与 `config`，不写 `name`，`name` 已经在前一层声明过。替换后整条 config 变成新值，旧字段不会残留。

这条规则有个反直觉的后果：你没法只改一个嵌套字段。想给某个插件加一个 option，必须把它的完整 config 写出来。设计上这是刻意的，半个 config 的来源比完整覆盖更难推理。

### 插入

```yaml
- insert:
    - id: my-tool
      name: '@me/dsh-tool-mytool'
      config: { endpoint: 'https://example.test' }
```

插入的行 id 必须唯一。重复 id 会让后面的层无法确定改哪一条。

### 禁用

```yaml
- id: plugin-manager
  disabled: true
```

`disabled: true` 让这一行不加载，但保留 id，后续层可以再把它打开，或者替换它的 config。禁用不是删除，这是分层能收敛的前提：高层可以说「我不要它」，而不需要知道低层写了什么。

## 5. preset：再往里钻一层

`preset` 是「一组插件配置的命名集合」，作用于某个 agent 的 `config.plugins`，而不是顶层行。

```yaml
- id: agent-preset
  config:
    presets:
      reviewer:
        plugins:
          - id: code-review
            name: '@me/dsh-tool-review'
```

所以装配有两套寻址空间：顶层行（`id` 全局唯一）与 preset 内的行（`id` 在该 preset 内唯一）。同名 id 在不同 preset 里指向不同插件是合法的。

`preset` patch 沿用同样的层优先级：profile 的 preset 定义 → home patch 的 preset 覆盖 → `--patch`。这条让「同一个 preset 名在不同机器上指向不同实现」成为可行配置，而不是意外。

## 6. 看一眼生效的树

```sh
dsh --profile web --dump-config
```

它打印装配后的完整条目列表。任何一条都能被你的 patch 替换，没有「不可覆盖」的行，包括模型适配器与会话存储。

这是「无特权内核」在配置层的体现：如果某一行不可覆盖，它就成了事实上的内核。

## 7. HMR 的边界

`dsh-hmr` 默认只监视配置文件，模块根是 opt-in（`root: []`）。

| 变化 | 行为 |
| --- | --- |
| profile 的 `cordis.patch.yml` | 重新装配，按 id 差分，只重载受影响的 fiber |
| 插件源码（`root` 非空时） | 重新构建并重载该插件 |
| 服务实现变化 | 依赖方 epoch 变 INACTIVE，自动卸载重载 |

第三条不是 HMR 的特权，是 Cordis 的常规行为，这也是为什么「改一个插件的实现」不需要重启进程：依赖方会因为提供者 uid 变化而重载。

HMR 在 headless / sdk / acp 里默认关闭，`sdk-minimal` 干脆不含它。理由是这些形态的进程生命周期短，热重载只会增加不确定性。

{{< fig "diagrams/04-hmr-scope.svg" "HMR 的两级作用域：配置变化重装配，模块变化重载插件；两者都落到 Cordis 的 fiber 生命周期上。" >}}

## 8. 设计取舍

| 取舍 | 选择 | 代价 |
| --- | --- | --- |
| 合并 vs 替换 | 替换整条 config | 改一个字段要写全，文件更长 |
| 层数 | 五层，但每行最多两层覆盖 | 表达力受限，换来可追溯 |
| 模式差异 | 各模式 bundle 重写，而非 base 里加条件 | 有重复配置 |
| 行顺序 | 不承载语义 | 读者不能靠位置推断加载顺序 |
| 禁用 | 保留 id，可再启用 | 需要区分「禁用」与「不存在」 |
| 例外 | 允许 `sdk-minimal` 不套 base | 多一套需要同步维护的配置 |

## 9. 踩坑点

1. 以为 patch 会深合并。它替换整条 config，漏写的字段就是没了。
2. 在 base 里加模式相关字段。应该放进各模式 bundle，否则另一模式会继承到不该有的默认值。
3. 依赖 YAML 行序。激活由依赖满足驱动，顺序无意义。
4. 用 `disabled` 当删除。删除会丢掉 id，后续层无法再引用。
5. 在 preset 里复用顶层 id。两套寻址空间不互通。
6. 以为 `--patch` 只能改用户层。它能改任何 id，包括 base 的行。
7. 开 HMR 后改源码没生效。默认只监视配置，模块根要显式加到 `root`。
8. 在 `!!js` 谓词里做重活。它在装配期同步求值，抛错会让整个 profile 起不来。

## 10. 自己搭的时候

- 把装配做成数据，不要做成代码分支。`if (mode)` 一旦出现，第三种模式就会变成嵌套 if。
- 给每一行一个稳定 id。没有 id 就没有寻址，也无法做差分重载。
- 限制每行的覆盖层数。两层是「作者默认 + 用户意图」的自然分界，再多就没人能追溯。
- 让「禁用」可逆。保留 id、只改布尔，高层才能在不了解低层的情况下表达意图。
- 提供 `--dump-config`。装配是五层叠加时，用户唯一能信任的就是「打印出生效值」。

## 11. 源码索引

| 位置 | 内容 |
| --- | --- |
| `docs/architecture.zh.md:20-40` | profile / bundle / patch 的层序 |
| `packages/bundle/base/cordis.patch.yml:1-20` | 替换语义与「模式行不进 base」的注释 |
| `packages/bundle/base/cordis.patch.yml:20-80` | 行形状、`!!js` 谓词、`disabled` |
| `packages/bundle/base/package.json` | `dsh.bundle` 声明与导出 |
| `packages/bundle/web-app/` | web 模式增量 |
| `packages/bundle/sdk-minimal/` | 不套 base 的显式配置树 |
| `packages/boot/app-boot/README.zh.md` | profile 装配与启动 |
| `packages/boot/hmr/` | 配置文件监视与重载 |
| `packages/preset/agent-preset/src/index.ts` | preset 与 `config.plugins` |
| `packages/boot/cmdline/src/index.ts` | `--profile` / `--patch` / `--dump-config` |

## 12. 最后

五层叠加听起来复杂，但它解决的是一个具体问题：同一个产品要在五种形态下启动，而每种形态只该看见自己需要的插件。

行 id + 整条替换 + 两层上限，三个约束合起来把「配置从哪来」变成可回答的问题。没有这三个约束，五层就会退化成「谁最后写谁赢」的碰运气。

系列导航：[第五季导览](/posts/dsh-season-five.html) · [Cordis 内部](/posts/dsh-cordis-internals.html) · [诊断现场](/posts/dsh-diagnostics.html)
