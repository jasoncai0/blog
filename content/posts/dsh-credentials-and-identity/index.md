---
title: "凭据与身份：谁能代表用户"
date: "2026-11-14T19:00:00+08:00"
summary: "API key、OAuth、SSH 私钥、平台令牌四类凭据的存放与取用边界，以及 agent 在什么情况下算「代表用户」。"
description: "拆 credentials 与 identity 两个服务：解析顺序、刷新、脱敏、越权路径与审计。"
tags: ["harness", "architecture", "agent"]
slug: "dsh-credentials-and-identity"
---

> 依据：源码 commit `fcc0ad46`（版本 `0.2.2-rc.1`），`docs/subsystems/credentials.zh.md`（493 行）、`docs/subsystems/session-telemetry.zh.md`（212 行）、`packages/credentials/{credentials,credentials-local,authorization,deepseek-account,deepseek-account-platform}/`、`packages/api/settings-controller/src/credentials.ts`、`packages/subprocess/subprocess/src/index.ts`、`packages/mcp/mcp-client/src/credentials.ts`、`packages/identity/anonymous-user-id/src/index.ts`、`packages/client/connection/src/browser-auth.ts`。证据等级：A（逐行读过实现与子系统文档）；审计一节的部分结论来自全仓检索，属 B。

Agent 在会话里说：「我已经用你的 GitHub 账号建了 PR。」用户的第一反应通常是：谁允许的？

这个问题没有单一答案，因为它混了三件事：凭据从哪来、agent 在哪个进程里跑、宿主把哪把钥匙交给了它。DSH 把前两件拆成两个 seam：`ctx.credentials` 管「这个值是什么」，`ctx.identity` 与各处的归属字段管「这是谁」。这一篇按调用顺序拆开看。

## 0. 结论

五类凭据，五种存放位置：

| 凭据 | 存在哪 | 谁能读 | 失效方式 |
|---|---|---|---|
| API key | `$DSH_HOME/.credentials.yaml`，或进程环境、`.env` | 同进程的 credentials 消费方 | 用户 `unset`；文件被覆盖 |
| DeepSeek 账号 grant | 同一文件的 `records` 段，kind=`grant` | Host 的 account 服务；模型侧只拿 token | 服务端 401 后删除记录，或用户 signOut |
| OAuth 授权结果 | 插件私有 GrantRecord，走 `modifyRecord` | 注册了该 flow 的授权服务 | `authorization/settled` 里声明 |
| SSH 私钥 | 用户自己的 OpenSSH 配置 | 不经过 harness | 与 harness 无关 |
| 浏览器会话 | credentials 记录 `client-connection/browser-session` | Host connection carrier | 过期、签名不符、token 不匹配 |

四条更硬的结论：

- 解析顺序是文件、进程环境、项目 `.env`、用户 `.env`，没有系统 keychain 这一层。文件权限 `0600` 就是边界，不是辅助。
- DeepSeek 账号没有 refresh token。代码里没有刷新调用，token 还有效与否由服务端 401 决定，失败后模型什么都看不到，因为请求没有到达模型。
- 脱敏没有中心拦截器。它由三处默认值组成：子进程环境裁剪、MCP 模板绑定、遥测 waterfall。工具自己打印出来的内容不在其中。
- 审计没有专门的账本。能查的只有事件与会话日志，前者记「配置变了」，后者记「发生了什么」，都不直接回答「谁用了哪把钥匙」。

## 1. 两套键空间：引用与记录

`ctx.credentials` 里同时存在两种键，解决的问题不同：

| | `CredentialRef` | `CredentialKey` |
|---|---|---|
| 形状 | branded 的 POSIX 环境变量名 | 插件与 id 组成的记录键 |
| 语义 | 「这个环境变量名后面是什么」 | 「这个插件为这个 id 持有什么」 |
| 是否层叠 | 是，文件、进程环境、`.env` 依次回退 | 否，记录存在即全部事实 |
| 写路径 | `set` / `unset` | 只有 `modifyRecord` |
| 空值 | 任何地方都视为不存在 | 不适用 |

引用这一半有三个方法：`resolve(ref)` 返回 `{value, source}`，`describe(ref)` 返回 `{configured, source, writable}`，`set`/`unset` 写托管文件。`describe` 的返回类型里没有值字段，所以配置界面可以整块跨 Remote wire，不需要为「看不见 secret」单独设计协议。

记录这一半只有 `readRecord`、`describeRecord`、`listRecords`、`modifyRecord`、`deleteRecord`。`readRecord` 原样返回 owner 写进去的 payload，不解释 grant 的结构；解释权归写它的插件。

为什么记录只有一条写路径：正确的写入依赖当前值。token 轮换是「读当前值、决定新值、替换」，三个动作必须在同一个锁里。如果暴露一个盲写的 `putRecord`，两个进程同时轮换同一个 refresh token，后写的会覆盖先写的，先写的那次轮换在服务端已经生效，本地却丢了新 token，下一次请求必然失败。`modifyRecord` 把这段临界区收进实现，跨进程排他由后端保证。

{{< fig "diagrams/01-two-keyspaces.svg" "两套键空间。左边是引用：文件、进程环境、项目 .env、用户 .env 依次回退，每次操作重新解析。右边是记录：没有层叠，存在即事实，唯一写路径是锁下的 read-decide-replace，用于 token 轮换与授权结果。" >}}

seam 级还有一条规则：空存储值在任何地方都视为不存在。`resolve` 跳过它，`describe` 报未配置，`set('')` 直接拒绝并提示改用 `unset`。否则一个空字符串会伪装成「已配置」，消费方拿着空 key 发请求，错误信息会指向网络而不是配置。

## 2. 解析顺序：文件、进程环境、两层 .env

本地 provider 的优先级写在文件头（`credentials-local/src/index.ts:1-20`）：

```text
$DSH_HOME/.credentials.yaml        provider 管理，set/unset 写这里
> 继承的进程环境                    只读回退
> <invocation cwd>/.env            只读回退
> $DSH_HOME/.env                   只读回退
```

实现就是三段短路（`index.ts:624-635`）：

```ts
override resolve(ref: CredentialRef): Promise<ResolvedCredential | undefined> {
  const stored = this.values.get(ref)
  if (stored !== undefined) return Promise.resolve({ value: stored, source: 'file' })
  const inherited = this.inherited(ref)
  if (inherited !== undefined) return Promise.resolve({ value: inherited, source: 'env' })
  const fallback = this.dotenvFallback(ref)
  if (fallback !== undefined) return Promise.resolve({ value: fallback.value, source: fallback.source })
  return Promise.resolve(undefined)
}
```

顺序的理由不是「文件更可信」，而是「用户显式保存的值必须在所有 provider 上一致生效」。CI 注入的环境变量只在该文件没有对应值时生效，所以本地调试用 CI key、产品里用保存的 key，两者不会互相打架。项目 `.env` 排在进程环境之后、用户 home 之前：产品信任启动目录，但用户在产品里保存过的值优先于 checkout 里碰巧带的 key。

这个顺序里没有 keychain。想要 keychain 就得再写一个 provider，实现同一组方法，然后由组合选择；仓库自带的实现只有文件加三层环境。文件的安全边界是权限：读取之前先 `assertOwnerOnly`，发现 group 或 other 有任何权限位就拒绝启动并提示 `chmod 600`。它防的是「同机其他用户读走 secret」，不防同用户的恶意进程。

热更新的机制是「每次操作重新解析」。`resolve` 不缓存，文件变化通过 chokidar 观察，事件到达后走 `operations` 队列重新读盘、整体替换快照。写与 watcher 共用一条队列，写之前先在锁内 `reconcileFromDisk`，所以外部编辑不会在写回时被覆盖。自我写入靠文本比较抑制，避免自己触发自己的 watcher。

watch 配置默认开启，debounce 默认 100ms（`awaitWriteFinish.stabilityThreshold`），watcher `ready` 之后再补一次 reconcile，堵住「初次读取与 watcher 生效之间写入」的窗口。`credentials/reference-updated` 只在 set、unset 或观察到外部编辑时发；进程环境的变化不可观测，所以永远不会为它发事件。

自己搭 Agent 时，先把「谁能写这个文件」定下来。DSH 的写法是：文件是唯一可写源，所有写入经过同一把锁与同一条队列，读路径永远不缓存。很多实现把 key 缓存在内存里，改配置要重启；一旦支持热更新，就必须同时处理并发写与自写抑制，否则会出现「界面显示已保存、实际被旧快照覆盖」。

## 3. OAuth：没有 refresh token 的一次性 grant

DeepSeek 账号的凭据是一条 grant 记录：token 加 issuer，存在 `records` 段里。登录流程从 `startSignIn(client, callbackOrigin, loginSource)` 开始，浏览器完成授权后回调，记录被写入；之后每次请求前 `resolveToken(url)` 解析。

`resolveToken` 有一道明确的闸门：只对 provider 允许的 inference origin 返回 token，URL 里带 userinfo 的拒绝，`dsh_mock_` 前缀的 mock token 拒绝，生产 origin 拒绝 localhost issuer。模型或工具无法通过构造一个 URL 把 token 引到别处。启动时 `Service.init` 还会检查已存 grant 的 issuer，如果不等于当前 origin，直接 `deleteRecord`，避免换了部署环境后继续用旧环境的凭据。

{{< fig "diagrams/02-account-token-flow.svg" "账号 token 的生命周期。登录写入 grant，请求前按 origin 解析 token，服务端 401 触发删除记录与 session-expired，agent 错误监听器再发 model-sign-in-required。代码里没有 refresh 分支，续期只能重新登录。" >}}

这里没有 refresh token，也没有「过期前多久刷新」的逻辑。代码里不存在刷新调用，token 是否有效完全由服务端判定。这个选择把状态机砍掉一大半：不需要存 refresh token，不需要处理刷新失败，不需要决定「刷新时旧 token 还能不能用」。代价是过期后的恢复只能靠用户重新登录。

失败之后模型看到什么：什么都看不到。请求在 `resolveAuth` 阶段就失败了（`llm-deepseek-account/src/index.ts:39-52`）：

- 没有 token 时抛 `LlmError('Sign in to DeepSeek to use the account provider. The request destination must allow account authentication.', 'ACCOUNT_SIGN_IN_REQUIRED')`。
- 服务端返回 401 时抛 `ACCOUNT_TOKEN_INVALID`，并调用 `rejectToken` 删掉被拒的那一个 token。

两种情况都不会伪造一条 assistant 消息，也不会把错误文本塞进对话历史。`agent/error` 监听器看到 `ACCOUNT_SIGN_IN_REQUIRED` 后发 `deepseek-account/model-sign-in-required`，界面据此弹登录提示；`signed-out` 事件会取消所有「最近一次请求走账号路由」的运行中任务，保留收件箱。代码里没有在 401 之后自动改用 API key 的路径，provider 是独立注册的 `deepseek-account`，切换要用户自己改设置。

并发登录用「一次一个尝试」处理。`authorization.begin(key)` 在已有尝试时返回 `ALREADY_IN_FLIGHT`，第二次调用被拒绝而不是排队；`cancel(key)` 独立于请求的 AbortSignal，取消的是 key 上的尝试。账号详情刷新另有一个 lifetime 令牌：请求发出时记下当前 lifetime，回来时如果 lifetime 已经换了，结果直接丢弃，避免旧响应把新状态盖掉。失败时允许回退到「同一 grant 上一次成功的读取」，注释写得很直白（`deepseek-account-platform/src/index.ts:199`）：只有同一 grant 的最后一次就绪读取可以顶替失败的刷新。

## 4. 并发写：锁下的 read-decide-replace

`modifyRecord` 的契约是「`mutate` 看到的是写入独占那一刻的记录」。本地实现（`credentials-local/src/index.ts:672-720`）按这个顺序做：

1. 进入 provider 的操作队列，队列里同时有 watcher reload 与别的写。
2. 建目录，拿文件锁 `withFileLock(filename, ...)`。
3. 锁内 `reconcileFromDisk()`，把可能被其他进程改过的内容读进来。
4. 调 `mutate(current)`，`undefined` 表示不改。
5. 用读路径同样的规则校验新记录，拒绝持久化一个下次启动会解析失败的值。
6. 只改自己那把 key，保留其他条目与注释，`writeFileAtomic` 落盘。
7. 更新内存快照并发 `credentials/record-updated`。

第 3 步是重点。如果省掉它，两个进程各自基于自己启动时读到的快照改文件，后写的会覆盖先写的。跨进程锁由 `withFileLock` 提供，写文件用原子替换，读方要么看到旧文件要么看到新文件，不会看到半个 YAML。

授权服务是另一套并发：一个 key 注册一个 flow，重复注册返回 `DUPLICATE_FLOW`；`begin` 一次只允许一个尝试；flow 写自己的 GrantRecord；结束后发 `authorization/settled(key, settlement)`。listener 抛错会被兜住并记录，不改变已经提交的结果。

记录枚举 `listRecords` 只返回值以外的信息：键、kind、是否可写。配置界面靠它列出「这个用户授权了什么」，也靠它发现卸载插件留下的孤儿记录。引用的枚举不存在，因为引用有哪些由设置 schema 决定。

## 5. 脱敏：三层默认值，没有中心拦截器

{{< fig "diagrams/03-redaction-layers.svg" "三层脱敏与一个缺口。子进程环境在 spawn 时裁剪，MCP 模板绑定只注入显式声明的引用并在诊断里替换，遥测记录过 waterfall 后只导出副本。工具自己打印的内容没有中心过滤，这是设计上的缺口，不是遗漏。" >}}

第一层在进程边界（`packages/subprocess/subprocess/src/index.ts:44-84`）。`scrubbedParentEnv()` 删除两类变量：名字匹配 `SENSITIVE_ENV_PATTERN = /KEY|PASSWORD|SECRET|TOKEN/i` 的，以及所有大小写不敏感的 `DSH_*`。PATH、HOME、locale、代理保留，显式传入的 env 在裁剪之后合并。所有走 subprocess seam 的子进程都拿到这份环境：子 agent 的 CLI、MCP stdio server、hook 命令。

为什么不能只靠工具自觉：工具是任意代码，MCP server 是第三方进程。让每个工具自己记得不读 `DEEPSEEK_API_KEY`，等于把安全边界交给生态。在 spawn 处裁剪是唯一可执行的位置，因为那是 harness 最后一次控制环境的机会。

第二层在 MCP 客户端（`packages/mcp/mcp-client/src/credentials.ts`，96 行）。配置里写 `${credential:REF}`，`expandTemplates` 在连接时把 command、args、env、cwd 或 url、headers 里的占位符替换成解析结果。解析出的值及其 `encodeURIComponent` 形式都进一个实例生命周期的 Set，`redact()` 把它们全部替换成 `[redacted]`，诊断信息走 redact。缺 credentials 服务时抛 `McpLocalError`，连接状态停在 `waiting-credentials`，不会带着未展开的占位符去启动。

这一层的语义是「显式声明才给」。MCP server 拿不到环境里的其他 secret，只能拿到配置里点名的引用。代价是配置必须写全，漏写一个引用就连接失败，而不是悄悄回退到父进程环境。

第三层在遥测导出（`docs/subsystems/session-telemetry.zh.md:142-144`）。每条记录过 `session-telemetry/record` waterfall：seam 自身不带任何规则，监听器可以改写 `next()` 的结果；不调 `next()` 就替换下游；监听器抛错则这条记录被扣下，fail-closed。改写只作用于导出副本，权威会话日志永不改写。这条设计把「什么算 secret」交给部署方，seam 只提供拦截点。

设置界面另有一处 `redactSecrets`：`describe({redactSecrets: true})` 返回的视图不含 secret 字段，schema 决定哪些字段算 secret。它保护的是配置读取，不保护工具输出。

缺口要说清楚：如果模型让 shell 执行 `env`，或者读一个包含 token 的文件，输出会原样进入会话。DSH 没有在工具结果进入模型之前做中心过滤。这不是忘了做，而是中心过滤需要知道每个工具的语义，误伤与漏检都不可避免；能做的都在边界上做了：spawn 时不给、MCP 只给点名的、导出时不带。

自己搭 Agent 时，把脱敏放在「值跨边界」的三个位置：进程创建、网络连接、日志导出。不要放在工具实现里，也不要指望模型不看。工具输出这一层如果要补，只能是按工具类型白名单加结构化结果，通用正则替换一定会漏。

## 6. 身份：匿名 id、会话归属、多用户

DSH 里没有一个贯穿全局的 `userId`。不同的 id 回答不同的问题：

| id | 作用域 | 来源 | 用途 |
|---|---|---|---|
| 匿名用户 id | 一个 harness home | 随机 UUIDv4，存 `.anonymous-user-id` | 遥测与反馈去重 |
| 会话 id | 一个会话 | 创建时生成 | 日志、恢复、fork |
| `parentSession` + `delegationDepth` | 会话头 | 子 agent 创建时写入 | 归属与深度限制 |
| `origin: 'subagent'` | 会话头 | 子 agent 创建时写入 | 区分人类会话与委派会话 |
| IdentityTriple | 一个租户 | liveshare 协议 | 协作场景的授权主体 |

匿名 id 的规则很克制（`packages/identity/anonymous-user-id/src/index.ts`）：每个 harness home 一个 UUID，绝不从 hostname、网络地址、git remote 派生；共享同一个 `$DSH_HOME` 的进程拿到同一个 id；删掉文件，下次启动就是新身份。并发首次启动用 `wx` 独占创建，输的那个进程重读赢家；home 只读时退回内存 id，不阻塞遥测。它不是一个账号，只是「同一个安装」的代号。

会话归属靠 header，不靠用户 id。子 agent 的 header 带 `parentSession`、`origin: 'subagent'`、`delegationDepth`，`isSeeded` 标记它是否从种子会话开始。这些字段进 header 不进事件日志，因为它们描述会话本身，不描述会话里发生了什么。凭据层面没有对应的归属：同进程的子 agent 共享同一个 credentials 服务，没有「这个子 agent 只能用这把 key」的机制。

多用户只在 experimental 的 liveshare 里出现。协议定义 `IdentityTriple {providerId, tenantId, subjectId}`，`identityKeyOf` 把三段百分号编码后用 `:` 连接，tenant 缺省时用 `default` 占位，`normalizeIdentity` 要求 provider 与 subject 都存在，`requireIdentityTenant` 在需要租户隔离的路径上强制要求。`IdentityProvider` 是一个通用的浏览器 OAuth 契约（给 authorizationUrl、换 code），仓库里的 Mock provider 在文档里明说它不是认证边界。

浏览器会话是另一条身份线（`packages/client/connection/src/browser-auth.ts`）。32 字节随机 secret 存成 credentials 记录 `client-connection/browser-session`，cookie 是 `v2.<payload>.<HMAC-SHA256>`，带 `HttpOnly`、`SameSite=Strict`、`Max-Age` 与 `Expires`；启动 token 只在进程内 WeakMap 里，不落盘。校验用 `timingSafeEqual`，payload 里的 authority 与请求 authority 必须一致。这条链回答的是「这个浏览器窗口有没有资格连这个 host」，与会话归属是两件事。

## 7. 越权路径清单

「代表用户」在 DSH 里的定义是：这个进程能解析到哪些凭据。模型本身没有被授予身份，它只能通过工具与子进程去使用进程已有的能力。逐条看：

| 路径 | 实际边界 | 结论 |
|---|---|---|
| 同进程子 agent / fork | 共享 `ctx.credentials`，可解析父 agent 能解析的全部引用 | 有继承，没有 per-agent 作用域 |
| 子进程子 agent（claude-code、codex、dsh-sdk、acp） | 父环境经 `scrubbedParentEnv()`，显式传入的凭据在裁剪后合并 | 默认不带，点名才给 |
| 远程沙箱 / SSH | host 别名由部署配置，含 user、key、known_hosts；`BatchMode`、严格 host key、禁 agent forwarding | 模型不能选主机与私钥 |
| MCP server | 只有配置里 `${credential:REF}` 点名的引用，诊断已脱敏 | 显式绑定，不能扫环境 |
| 工具输出 / 日志 | 工具打印什么，模型就能看到什么 | 无中心过滤 |
| 浏览器 / Host connection | 32 字节 secret 记录 + HMAC cookie + 一次性启动 token | 与会话身份分离 |

第一条最容易被忽略。fork 与应用内子 agent 都跑在同一个进程里，共享同一个 Cordis 上下文，所以它们不需要「传递」凭据，本来就能拿到。代码里没有按 agent 划分的凭据作用域。如果产品需要「这个子 agent 只能读不能写」，限制要加在工具层，而不是凭据层。

第二条的细节值得抄：子 agent CLI 的启动环境是裁剪后的父环境，配置里显式给了 `DEEPSEEK_API_KEY` 才转发。这条规则让「默认安全」与「需要时可用」共存，代价是每个需要凭据的子 agent 都要在配置里写一行。

第三条是 SSH 的边界：`packages/ssh/ssh/src/index.ts` 的 `Config` 只有 host 别名与 helper 启动参数，注释写明「部署方拥有的 SSH 身份，模型参数不能选择这些值」。隧道里每个流用 256 位 TLS PSK，密钥只走管理 RPC，不作为流前缀发送。私钥始终在用户的 OpenSSH 配置里，harness 既不读也不转发。

第四条已经在上文拆过。补一个细节：模板值同时保留原值与 URL 编码值，所以 `redact()` 能覆盖两种出现形式；诊断里不会出现半个编码后的 token。

## 8. 审计：谁用了哪把钥匙

全仓检索 `audit` 只命中 experimental 的 liveshare，没有凭据使用审计。能查到的记录有三类：

- `credentials/reference-updated(ref)`：引用被 set、unset 或观察到外部编辑。它记录配置变化，不记录使用。
- `credentials/record-updated(key)` 与 `authorization/settled(key, settlement)`：记录写入与授权结果。
- `deepseek-account/*`：登录、过期、登出。

会话日志能回答「哪个工具在什么时候做了什么」，但里面没有「用了哪把 key」。请求头、token 都不进事件日志。这是有意的：日志要能被导出、被模型读取、被协作方共享，secret 进去就收不回来。

{{< fig "diagrams/04-authority-paths.svg" "谁在什么边界上能代表用户。内圈是同进程的 agent 与 fork，共享 credentials 服务；外圈是子进程、MCP、SSH 与浏览器，每一层只拿到显式声明的部分。右下角是审计缺口：事件记录配置变化，不记录每次使用。" >}}

如果要在自己的 Agent 上补审计，最小可用的一组字段是：时间、会话 id、凭据键名（不是值）、操作类型、目标 origin、结果。写在工具或 provider 的调用点，不写在模型可见的输出里。把值写进审计日志，等于把 secret 复制到一个更少人看着的地方。

## 9. 设计取舍

- **没有 keychain**：文件加 `0600` 可移植、可检查、可备份，跨平台行为一致。代价是对同用户的恶意进程没有防护，而这本来也是 keychain 在同用户下的弱项。
- **托管文件优先于环境变量**：用户在产品里保存的值必须处处生效。代价是 CI 注入的 key 可能被本地保存的值遮住，排查时要先看 `source`。
- **没有 refresh token**：状态机简单，失败路径只有「重新登录」。代价是长时间运行的任务会在 token 失效时中断。
- **一次一个授权尝试**：拒绝而不是排队，避免两个浏览器窗口同时完成授权后互相覆盖。代价是用户要手动取消旧尝试。
- **`modifyRecord` 是唯一写路径**：把 read-decide-replace 收进实现，跨进程轮换安全。代价是插件不能盲写记录，必须提供一个纯函数。
- **脱敏放在边界而非中心**：spawn、MCP 绑定、遥测导出各自负责，组合性好。代价是工具输出这一层没有保护。
- **匿名 id 按 home 作用域**：同机多用户不会互相串，删文件即换身份。代价是无法跨设备去重。
- **fork 不隔离凭据**：实现简单，行为可预期。代价是无法在一个进程内做细粒度授权。

## 10. 踩坑点

- 把 `.credentials.yaml` 的权限改成 `0644` 图方便，provider 会在读取前直接拒绝启动，并提示 `chmod 600`。
- 用 `set(ref, '')` 清空一个 key。空值被拒绝，正确做法是 `unset`，否则 fallback 层不会重新生效。
- 改了进程环境里的 key 却期待 `reference-updated` 事件。进程环境不可观测，只有文件变化会触发。
- 手工读改写 YAML，绕开 `withFileLock` 与 `reconcileFromDisk`。两个进程同时改会丢写，注释也会被重排。
- 以为 `resolveToken` 会为任意 URL 返回 token。它只对 provider 允许的 inference origin 返回，带 userinfo 或非白名单 origin 一律拒绝。
- 401 之后用同一个 token 重试。token 已经被 `rejectToken` 删除，重试只会再失败一次，正确路径是重新登录。
- 在 MCP 配置里写 `${credential:REF}` 却忘了挂 credentials 服务。连接会停在 `waiting-credentials`，不会带着字面量占位符启动。
- 自己把 MCP 的模板值打进日志。`redact()` 只处理它自己知道的那些值，你新加的日志行不在保护范围内。
- 以为遥测 listener 抛错只是少一条日志。它是 fail-closed，整条记录被扣下，排查时要看 logger。
- 以为 signOut 会清掉所有凭据。它移除账号 grant，保留 API key；provider 侧的撤销在后台进行。
- 以为 fork 出来的子 agent 有独立身份。它共享同一个 credentials 服务，能解析父 agent 的全部引用。

## 11. 源码索引

- `packages/credentials/credentials/src/index.ts:150-260`：两套键空间、空值规则、`modifyRecord` 的锁语义。
- `packages/credentials/credentials/src/types.ts`：`CredentialRef`、`CredentialKey`、`ResolvedCredential`、`CredentialInfo`。
- `packages/credentials/credentials-local/src/index.ts:1-20,516-720`：四层优先级、resolve/describe、锁内 read-modify-write。
- `packages/credentials/credentials-local/src/index.ts:127-146`：`assertOwnerOnly` 与 `chmod 600` 提示。
- `packages/credentials/authorization/src/`：flow 注册、单尝试、`authorization/settled`。
- `packages/credentials/deepseek-account/src/account-tasks.ts:18-33`：`model-sign-in-required` 的触发与 signed-out 取消任务。
- `packages/credentials/deepseek-account-platform/src/index.ts:170-240`：grant 校验、issuer 检查、lifetime 丢弃旧响应。
- `packages/llm/llm-deepseek/src/credential.ts`：credentials 服务权威、`resolveToken`/`rejectToken` 的包装。
- `packages/llm/llm-deepseek-account/src/index.ts:39-52`：`ACCOUNT_SIGN_IN_REQUIRED`、401 转 `ACCOUNT_TOKEN_INVALID`。
- `packages/api/settings-controller/src/credentials.ts`：`describe` 批量上限、set/unset、值只单向跨 wire。
- `packages/subprocess/subprocess/src/index.ts:44-84`：`SENSITIVE_ENV_PATTERN` 与 `scrubbedParentEnv()`。
- `packages/mcp/mcp-client/src/credentials.ts`：`${credential:REF}` 展开、生命周期 Set、`redact()`。
- `packages/identity/anonymous-user-id/src/index.ts:1-100`：UUID 来源、`wx` 抢建、按路径 memo。
- `packages/client/connection/src/browser-auth.ts`：浏览器会话 secret 记录、HMAC cookie、启动 token。
- `docs/subsystems/credentials.zh.md`：seam 契约、授权 flow、Remote 视图的规范描述。
- `docs/subsystems/session-telemetry.zh.md:142-144`：`session-telemetry/record` waterfall 的 fail-closed 语义。

## 最后

把「谁能代表用户」拆成三层之后，问题变得可回答：进程能解析哪些引用，边界在哪一层裁剪，事件里留下了什么。DSH 的答案是前两层做得比较硬，第三层刻意留白。硬的地方是文件权限、origin 闸门、spawn 裁剪、锁内轮换；留白的地方是审计与工具输出。

自己搭 Agent 时，先决定你要哪一种「代表」：同进程共享全部凭据，还是每个子 agent 一份受限集合。前者实现一天，后者要设计一套作用域模型与传递协议。选完再决定审计记到什么粒度，因为等出事再补日志，已经发生过的事情补不回来。

相关阅读：[执行环境](/posts/dsh-execution-environment.html)、[人在回路](/posts/dsh-human-in-the-loop.html)、[嵌入协议](/posts/dsh-embedding-protocols.html)。
