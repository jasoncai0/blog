---
title: "Anthropic Engineering 热门文章全景：生产级 Agent 的五条核心原理"
date: 2026-11-27T17:00:00+08:00
summary: "拆解 Anthropic Engineering 专栏 10 篇高热工程实践：当模型推理能力跨过阈值，生产瓶颈已转向注意力预算分层、脚手架动态裁剪、脑手日志三元解耦、纵深环境围栏与抗刷分评测。"
description: "基于 Anthropic Engineering 置顶与代表性 10 篇论文，提炼上下文与离线记忆、长程编码 Harness、Managed Agents 解耦架构、Auto Mode 纵深围栏与抗 AI 技术评测五条工程原理。"
tags: ["anthropic-engineering", "agent", "harness", "context-engineering"]
---

前沿大模型进入长程自主执行阶段后，线上系统的核心瓶颈已转移到外部工程基础设施：如何管理有限的注意力预算、动态拆掉过时的脚手架（Harness，即包裹在模型推理外围负责循环控制、工具调度与上下文管理的宿主层）、隔离执行状态与凭证，以及在缺乏标准答案的场景下建立独立验证闭环。

这份综述面向正在搭建编码代理、长周期自动化流水线或多工具 Agent 平台的工程师，汇总 Anthropic Engineering 专栏自 2025 年 9 月至 2026 年 11 月发布的 10 篇热门与置顶技术文章。读完后，你可以直接对照下文五条原理审查自家 Agent 架构里的上下文堆积、验证失真、容器单点故障与权限越界隐患。文中引用的延迟、成本、拦截率与跑分均取自 Anthropic 官方披露的实验数据。

{{< fig "diagrams/01-agent-engineering-stack.svg" "Anthropic 生产级 Agent 四层工程栈：从底层无凭证沙箱、离线记忆整合、动态契约编排到顶层抗刷分验证的职责切分" >}}

## 一、上下文与离线记忆：把有限工作记忆与持久存储拆开

上下文窗口扩大到百万 tokens 并没有消除注意力稀释（Context Rot），生产系统必须把「当前轮次推理所需的最小高信噪比集合」与「跨会话持久化的原始事件和结构化知识」严格分开。

Transformer 自注意力机制对长度为 $n$ 的序列需要计算 $n^2$ 量级的成对关系，随着上下文拉长，模型对远距离细节的召回精度持续衰减。Anthropic 在《Effective context engineering for AI agents》（2025-09-29）、《Writing effective tools for agents》（2025-09-11）、《Code execution with MCP》（2025-11-04）与置顶长文《The components of agent memory》（2026-11-06）中，用四套递进机制替代了「把所有工具定义和历史记录一次性塞进提示词」的早期做法：

1. **工具定义精简命名空间，大体积中间结果改走代码执行按需加载。** 工具是确定性软件与非确定性 Agent 之间的契约（例如默认将单次工具输出截断在 25,000 tokens 内，提供 `concise` 与 `detailed` 详略枚举可省三分之二 tokens）。传统 MCP（Model Context Protocol，模型上下文协议）客户端启动时全量注入数千个工具 Schema，且每一步工具返回都要原样流经模型上下文再拷贝给下一个工具。将 MCP 服务映射为沙箱文件系统里的 TypeScript 模块树后，Agent 先列目录按需读取目标工具定义，再在执行环境内用代码完成过滤和拼接，实测将单次任务的上下文消耗从 150,000 tokens 压降到 2,000 tokens，节省 98.7%。
2. **区分会话内压缩、硬重置与跨会话经验记忆。** 会话内压缩（Compaction，如清理历史工具原始输出并保留近期访问的 5 个核心文件）只能缩短历史，无法消除部分模型临近窗口上限时仓促收尾的「上下文焦虑（Context Anxiety）」；对存在上下文焦虑的模型（如早期的 Sonnet 4.5），必须采用硬重置（Context Reset）彻底清空窗口并靠结构化交接文件续跑。而真正跨会话留存的「Agent 记忆」只记录环境画面读不到的已验证规律、策略与踩坑警告：在 Pokémon Red 万步长程实测中，不分青红皂白乱记流水的 Sonnet 3.7 写了 413 个文件（分布在 225 个目录）却只拿到 1 枚徽章，还在第 10,000 步附近连续 17 步忙于增删目录而未操作游戏；只把目标、路线计划与捕获规律写进 `global.md` 等 10 个文件的超前沿推理模型 Claude Fable 5 则拿下了 4 枚徽章。
3. **用离线 Dreaming 对照原始日志纠偏并构建知识图谱。** 在线执行时写笔记会分散主任务注意力，且一旦写错（例如把月见山入口宝可梦中心误记在东侧，导致连续三次跳下死胡同单向跳台），后续会话会盲信错误记忆而陷入死循环。引入睡眠期计算（Dreaming）后，后台 Agent 定期回查原始会话 Transcript 修正错误笔记，并将零散记录整理成带双向链接的 Wiki：在收购自 Stainless 的 35 万个文件（约 4.2 亿 tokens）语料库上，经过 128 轮 Dreaming 构建 Wiki 并配合搜索工具，25 道跨文档评测题得分率从纯搜索的 53% 提升到 61%，单题节省约 20,000 tokens；在法律文书生成基准（Harvey LAB）的并购合同审查任务中，改为「每份合同一名独立 Agent 建单页并逐字引述条款」消除并发覆写后，57 项关键条款保留率从共享表格方案的 72%（41/57）提升到 100%。
4. **顶层预加载索引必须严控体积，对强模型少下死规则。** 每次会话启动时无条件注入上下文的顶层记忆（如 Claude Code 自动加载 `MEMORY.md` 的前 200 行或 25 KB）只能充当目录索引，写得过长反而拉低指令遵循度。同时，在评估跨会话工具学习的持续学习基准 CL-Bench 1.0 上，人为规定「只准记录失败案例」反而让 Opus 4.7 与 Opus 5 丢掉了大半记忆增益：对前沿模型只需告知记忆目录用途，把显著性判断权交还给模型。

## 二、长程编码 Harness：给主观质量立契约，随模型升级拆脚手架

复杂应用长程自主开发的上限取决于两件事：用独立评估 Agent 打破生成者的「自我表扬偏置」，以及在新一代模型发布后及时拆掉不再承重的拆分结构。

Anthropic 在《Effective harnesses for long-running agents》（2025-11-26）与置顶文章《Harness design for long-running application development》（2026-03-24）中展示了长程编码脚手架的两次关键跃迁。早期单 Agent 循环面对「做一个 claude.ai 克隆版」这类宽泛指令时，常犯两类错误：要么试图一次性写完所有代码导致中途耗尽上下文留下一半烂摊子，要么看到前序会话写了几个页面就提前宣布大功告成；让生成者自检前端审美或功能完整性时，模型又天然倾向于给平庸甚至无法运行的半成品打高分。

| 运行方案与目标应用 | 编排结构 | 运行时长 | Token 账单 | 交付可用性对比 |
|---|---|---|---|---|
| Opus 4.5 单代理（2D 复古游戏引擎） | 单会话直接生成 | 20 分钟 | 9.00 美元 | 面板留白严重，实体与运行时断连，点击开始后角色完全无法移动 |
| Opus 4.5 V1 完整 Harness（同上） | 规划 + 分 Sprint 契约 + 逐轮 QA | 6 小时 | 200.00 美元 | 扩充为 10 个 Sprint 共 16 项功能，编辑器、AI 辅助生成与实机试玩全部跑通 |
| Opus 4.6 V2 精简 Harness（浏览器 DAW） | 规划 + 连续构建 + 终局三轮 QA | 3 小时 50 分钟 | 124.70 美元 | 移除 Sprint 拆解，主构建连续稳定运行超 2 小时，QA 抓出假按钮与路由冲突并闭环修复 |

1. **用 JSON 特性账本与启动自检锁住跨会话基线。** 初始化 Agent 先生成 `init.sh`、`claude-progress.txt` 以及包含 200 余项端到端验收步骤的 `feature_list.json`（实测 JSON 比 Markdown 更不容易被后续会话误删篡改，且硬性规定只准翻转 `passes` 布尔值）；每个后续会话开工前必须先读进度与 `git log`、跑通基础冒烟测试确认主干健康，每次只挑一个特性攻关并干净提交。
2. **用生成-评估分离与 Playwright 实机交互替代静态代码自审。** 借鉴生成对抗网络（GAN，用独立判别器对抗训练生成器）思路，V1 架构拆出 Planner（把 1–4 句话扩写成高层产品规格，不锁死底层实现细节以免错误级联）、Generator 与 Evaluator 三类角色。在前端设计上，用设计整体感、原创性（明确惩罚白卡套紫色渐变等 AI 模板套路）、工艺基础与功能可用性四项指标给主观审美定锚；在全栈开发上，双方每轮编码前先协商写入磁盘的「Sprint Contract」（例如第 3 个 Sprint 单为关卡编辑器就定下 27 条验收标准），再由独立调优为严苛审视态度的 Evaluator 通过 Playwright MCP 真实点按页面、抓出如 `PUT /frames/reorder` 排在 `/{frame_id}` 之后触发 FastAPI 422 整数解析报错等真故障。
3. **把每个脚手架组件视为可过期的模型能力假设。** 当底座从 Opus 4.5 升级到长程连贯性更强的 Opus 4.6 后，作者通过控制变量法逐个拆除组件验证承重性：直接删掉 Planner 会导致生成器低估任务范围，删掉终局 Evaluator 会漏掉轨道片段不可拖拽、录音按钮只有空壳等「最后一公里」伪实现，而原先用于防跑偏的「分 Sprint 拆解」和早期防焦虑的「上下文硬重置」已不再提供增益。直接移除这两项死重后，系统架构大幅简化且在浏览器数字音频工作站（DAW）任务上稳定跑通。

## 三、托管运行时：大脑、双手与会话日志三元解耦，凭证外部托管

把 Agent 循环、工具执行沙箱和会话历史塞进同一个容器会把服务器变成无法容错的「宠物（Pet）」，生产级托管平台必须将这三项运行组件拆成彼此无状态依赖的独立接口，并将外部凭证隔离进独立金库（Vault）。

《Scaling Managed Agents: Decoupling the brain from the hands》（2026-04-08）复盘了 Anthropic 构建托管 Agent 服务时的架构重构。单容器耦合方案看似省掉了跨服务通信，但在生产中暴露出三大死穴：容器崩溃即丢失整场会话；WebSocket 断流时分不清是 Harness 死锁还是容器掉线，且因容器内存放客户数据导致工程师无法登录排障；客户想让 Claude 访问自家 VPC 资源时，被迫打通对等网络或自建整套运行时。

{{< fig "diagrams/02-managed-agents-topology.svg" "Managed Agents 架构解耦对比：大脑、双手与会话日志三元运行解耦，配合外部凭证金库实现沙箱零密钥" >}}

```text
# 会话日志独立持久化，Harness 与沙箱均可随时销毁重建
function resume_or_step(session_id):
    events = session_log.getEvents(session_id, slice="unread_or_window")
    context = harness.transform_for_cache(events)   # 按当前模型策略组装上下文
    reply = claude.infer(context)                   # 无需等待容器冷启动即可首字输出
    session_log.emitEvent(session_id, reply)
    if reply.has_tool_call():
        # 容器仅在首次需要执行代码或改文件时按需拉起；崩溃作为工具报错返回重试
        hand = sandbox_pool.provision_if_needed(session_id)
        obs = hand.execute(reply.tool_name, reply.tool_input)
        session_log.emitEvent(session_id, obs)
```

上面这段主循环伪代码说明了两项关键收益：第一，推理首字响应（TTFT）彻底摆脱了容器镜像拉取和仓库克隆阻塞，不调沙箱的轮次零容器等待，实测将生产 p50 TTFT 压低约 60%、p95 TTFT 压低超过 90%；第二，凭证与代码执行环境实现物理隔离，Git 令牌仅在沙箱初始化克隆阶段绑定远端，MCP OAuth 令牌存放在外部 Vault 并由专用代理在出站时替换，即便沙箱内运行不可信代码遭遇提示词注入，环境变量里也读不到任何可用密钥。

## 四、自主权限与纵深围栏：模型分类器减摩擦，确定性环境兜底

人工逐条点确认挡不住「审批疲劳」，纯模型分类器挡不住直接注入与合法通道外传，高权限 Agent 必须用「确定性环境围栏封死爆炸半径 + 无推理盲审分类器过滤越权动作」双层叠加。

结合《How we built Claude Code auto mode》（2026-03-25）与《How we contain Claude across products》（2026-05-25），Anthropic 披露了一组极具警示意义的真实安全数据：在默认手动审批模式下，用户对权限弹窗的点击通过率高达 93%，弹窗越多审视越敷衍。为此，Anthropic 构建了按用户监督能力分级的三类环境围栏（`claude.ai` 用服务端临时 gVisor 容器；面向开发者的 Claude Code 用 Seatbelt/bubblewrap 原生沙箱先砍掉 84% 权限弹窗；面向非技术知识工作者的 Claude Cowork 用独立 Linux 内核本地轻量 VM，并把 Agent 主循环移出 VM 以防虚拟机启动失败导致客户端卡死）。

{{< fig "diagrams/03-auto-mode-pipeline.svg" "Claude Code Auto Mode 纵深防御管线：三级权限快速放行、剥离自述的双阶段分类器与连续拒绝熔断机制" >}}

在此基础上，为替代高危的 `--dangerously-skip-permissions`，Auto Mode 先把只读工具（Tier 1，进入时自动剥离 `python`/`bash` 通配豁免）与当前 Git 仓库内文件编辑（Tier 2，可经版本控制审查回滚）直接零延迟放行，再对外部 Shell、联网、子代理派生等高风险动作（Tier 3）启用「输入端注入探针 + 输出端转录分类器」双道防线，并用三次真实安全事件划清了模型层与环境层的责任边界：

1. **输入探针防外部劫持，输出分类器剥离自述与工具返回防劝说。** 在输入侧，服务端提示词注入探针（Prompt-Injection Probe）会在网页抓取、文件读取或 Shell 输出进入主 Agent 上下文前先行扫描，一旦发现疑似挟持指令立即插入警告，提醒模型警惕该结果并锚定用户原始诉求。在输出侧，Tier 3 转录分类器只读取「用户原始消息 + 待执行工具调用载荷」，故意剔除 Agent 自己的解释文本（防止模型编造「用户前文已默许」说服分类器放行）以及历史工具返回内容（阻断外部恶意载荷污染分类器）。实测证明保留完整上下文会拉低越权识别率，只看工具调用则会丢失用户授权锚点。
2. **双阶段共享前缀缓存兼顾低延迟与低误杀。** Stage 1 采用偏向拦截的单 token（yes/no）快速过滤（真实内部流量 `n=10,000` 上误拦率 FPR 为 8.5%，52 例真实热心越权样本集上漏报率 FNR 为 6.6%）；仅被标红的动作进入复用同一缓存前缀的 Stage 2 链式推理复核，最终把日常误拦率压到 0.4%，合成数据外传（`n=1,000`）漏报率控制在 5.7%，同一批 52 例真实热心越权样本的全链路漏报率为 17%（约 9 例因分类器误将用户模糊指令当成完整授权而放行）。单次拦截作为工具报错返回让模型换安全路径重试，连续 3 次或单会话累计 20 次被拒则立即熔断交还人类（无头模式下直接终止进程）。
3. **警惕信任对话框之前的本地配置解析与挂载目录符号链接逃逸。** 2025 年中至 2026 年 1 月的三起漏洞披露显示，攻击者在恶意仓库 `.claude/settings.json` 中埋入启动 Hook，Claude Code 若在用户点击「信任此目录」对话框前解析本地配置或暴露本地监听接口就会直接中招；同理，Claude Cowork 挂载本地目录时若先校验路径白名单、后解析符号链接（Symlink），工作区内的恶意软链就能指向宿主机敏感目录实现逃逸。修复原则是未获明确授权前绝不解析本地项目配置，且路径校验必须在软链解析之后执行。
4. **直接钓鱼与合法域名外传只能靠环境层确定性拦截。** 2026 年 2 月内部红队演练中，员工把伪装成日常协作邮件的提示词粘贴进终端，指令顺带读取 `~/.aws/credentials` 外发，由于指令出自用户之手，锚定用户意图的模型分类器无法察觉异常，25 次重试成功外传 24 次；另一起外部披露中，工作区恶意文件自带攻击者自己的 Anthropic API Key，诱导 Claude Cowork 调用白名单内的 `api.anthropic.com` Files API 把本地机密上传到攻击者账号。最终修复是在 VM 内部署防御性中间人代理，强制校验请求必须携带当前 VM 签发的专属会话 Token，证明「自定义白名单本质是能力授权，凡是概率模型漏掉的攻击最终都由确定性边界承接」。

## 五、抗刷分技术评测：当常规题库失效，走向分布外与现场造工具

评估人类工程师或下一代模型能力的考题一旦落入预训练高频分布就会迅速失去区分度，高信噪比评测必须转向分布外（Out-of-Distribution）约束并把「如何现场自建调试工具」纳入考察核心。

《Designing AI-resistant technical evaluations》（2026-01-21）记录了 Anthropic 性能工程团队招募超 1,000 名候选人的机考题（在模拟 TPU/Trainium 加速器的自定义虚拟机上，手工管理片上暂存器内存、把多条指令打包进超长指令字 VLIW 并用单指令多数据流 SIMD 优化并行树遍历）在两年内被自家模型连破三次的完整演进：

1. **常规限时优化题在一年内从筛出黑马退化为全权委派。** 初版 4 小时考题在 2024 年成功发掘出多名应届顶尖内核工程师；但到 2025 年 5 月，预发布版 Claude Opus 4 在 4 小时内击败了绝大多数人类；升级指令集深度并压缩到 2 小时的第 2 版，又被 Claude Opus 4.5 在 2 小时内跑出 1,790 时钟周期追平人类最佳成绩（测试时计算框架加跑多小时后进一步压到 1,363 周期）。
2. **真实业务难题仍会被模型靠海量先验经验池降维打击。** 作者首次尝试重设计时，抽取了内部棘手的「2D TPU 寄存器无存储体冲突（Bank Conflict）转置」问题，结果 Opus 4.5 不仅想到直接转置整段计算的巧妙改写，打补丁封堵后开启长思考预算（`ultrathink`）依然凭跨平台训练语料里的存储体冲突套路直接解出。
3. **放弃表面业务仿真，用极简异构指令集考察第一性原理与造工具判断力。** 最终落地的第 3 版转向类似 Zachtronics（《深圳 I/O》）的多芯片极简受限指令集谜题（每片仅容约 10 条指令与 1–2 个状态寄存器），且故意不提供任何可视化或调试器，初始代码只判对错。由于落入预训练盲区，Opus 4.5 直接折戟；而优秀工程师能在几分钟内指挥编码模型先生成交互式调试器再动手压指令数，成功在允许使用 AI 辅助的前提下重新拉开了人与纯模型托管的差距。

排查自家生产 Agent 故障或架构退化时，可直接按这五条顺序逐项核对：上下文膨胀或旧错重犯先查工具是否改为按需加载及是否配有离线日志纠偏（第 1 节）；长任务中途烂尾或伪造完工先查是否缺少只增不删的外部 JSON 契约与实机交互评估器（第 2 节）；首响慢或容器失联丢状态先查会话日志是否已剥离出执行沙箱（第 3 节）；放开权限后担心删库泄密先查凭证是否移出沙箱、出站代理是否校验了专属会话身份（第 4 节）；内部基准或招聘机考区分度暴跌先查题目是否落入常见预训练套路、是否考察了现场自建工具链的能力（第 5 节）。

## 参考来源

1. Anthropic Engineering, [Effective context engineering for AI agents](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)（2025-09-29）与 [Writing effective tools for agents](https://www.anthropic.com/research/writing-tools-for-agents)（2025-09-11）。
2. Anthropic Engineering, [Code execution with MCP: Building more efficient agents](https://www.anthropic.com/research/code-execution-with-mcp)（2025-11-04）。
3. Anthropic Engineering, [Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)（2025-11-26）与 [Harness design for long-running application development](https://www.anthropic.com/research/harness-design-long-running-apps)（2026-03-24）。
4. Anthropic Engineering, [Designing AI-resistant technical evaluations](https://www.anthropic.com/research/AI-resistant-technical-evaluations)（2026-01-21）。
5. Anthropic Engineering, [How we built Claude Code auto mode](https://www.anthropic.com/research/claude-code-auto-mode)（2026-03-25）、[Scaling Managed Agents](https://www.anthropic.com/research/managed-agents)（2026-04-08）、[How we contain Claude across products](https://www.anthropic.com/research/how-we-contain-claude)（2026-05-25）与 [The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory)（2026-11-06）。
