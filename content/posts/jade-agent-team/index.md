---
title: "Jade 的 Team/TeamBoard：把单 Agent 变成可写小队"
date: 2026-11-26T00:40:00+08:00
summary: "Jade 1.4.0 落地了 Team/TeamBoard：MySQL 持久任务板、可写 teammate、Lead 审批和只读看板。这篇讲清楚任务模型、CAS、成员运行时、权限回传和验收边界。"
description: "从 DSH 的 Agent Team 对照出发，拆解 Jade 的 TeamService、MySQL 任务板、可写成员和 Lead 审批。"
tags: ["multi-agent", "harness", "architecture"]
---

Team 不是把 Agent 复制几份就能跑起来。

Jade 在 1.4.0 里加了一层 Team/TeamBoard：根会话是 Lead，任务写进 MySQL，teammate 是共享 workspace 的可写 `AgentRun`，权限和最终验收仍回到 Lead。这一篇不只贴接口，重点讲清楚四个容易做错的边界：任务为什么必须先落板、CAS 解决什么、可写成员和普通 subagent 差在哪、审批为什么要跨 run 回传。

## 0. 结论

Jade 的 Team 是**根会话上的持久任务板 + 可继续的成员运行时**，不是调度器，也不是容器集群。

三条设计判断：

1. **任务先落板。** 任务、owner、依赖、revision 存 MySQL；模型不能靠上下文记住分工。
2. **成员是可继续的 AgentRun。** 每个 teammate 有独立的 transcript、事件流、权限 broker 和 inbox，不是一次性 `delegate_task`。
3. **Lead 保留审批和最终验证。** teammate 可以写文件、编译、运行，但权限请求统一回到 Lead；最终回答和 workspace 验收仍由 Lead 负责。

Team 默认关闭。关闭时任务和历史成员保留，`team_*` / `team_spawn` 工具与 Team 入口隐藏。

## 1. 先看一次真实运行

测试根会话 `run-76b44c21` 里：

1. Lead 打开 Team，创建 `task-1 Audit PENDING`；
2. `team_spawn` 创建 worker，worker 返回 `child-done`，用 `send_message` 把 `worker-done` 送回 Lead；
3. 另一个 writer 调 `write_file`，run 进入 `WAITING_APPROVAL`；
4. Lead 在 Team drawer 看到 `perm-8a5bf30b`，选 allow once；
5. writer 继续执行，返回 `child-wrote`，状态 `SUCCEEDED`；
6. 重启 Jade 后，`task-1` 和 enabled 仍在，成员不在了——这是当前实现真实的冷成员语义。

这组流程里没有“后台自动放行”，也没有“任务完成自动叫醒下一个成员”。这两点都是故意的。

## 2. 架构：三条边界

{{< fig "diagrams/01-team-architecture.svg" "Jade 进程内的 Lead/TeamService/成员与外部 MySQL、审批边界。" >}}

图里三件事最重要：

1. **TeamService 在 Jade 进程内。** 它持有 root 到成员的运行时映射，任务本身不依赖成员在线。
2. **MySQL 是任务的 durable source of truth。** `jade_teams` 存开关和任务号，`jade_team_tasks` 存任务、owner、依赖和 revision。
3. **共享 workspace 不是隔离 workspace。** 所有 teammate 通过 `Workspace.open(root)` 指向同一个 run 目录，没有 worktree、没有容器。 `writeScopes` 只是提示，不会锁文件。

这也是为什么 Lead 必须保留最终验证：并发写同一文件时，框架不会帮你合并。

## 3. 任务模型

### 3.1 字段

```java
record TeamTask(
    String id,            // task-1, task-2...
    long revision,        // CAS
    String subject,       // 1..200
    String description,   // 0..16384
    TeamTaskStatus status,// PENDING, IN_PROGRESS, COMPLETED, DELETED
    String ownerId,       // team-* run id
    String ownerName,
    List<String> blockedBy,
    List<String> writeScopes,
    long createdAt,
    long updatedAt
) {}
```

`blockedBy` 和 `writeScopes` 以 JSON 列保存。没有单独依赖表：最多 256 个未删除任务，读全表做 DAG 校验足够。

### 3.2 MySQL

```sql
CREATE TABLE jade_teams (
  root_run_id VARCHAR(80) PRIMARY KEY,
  enabled TINYINT(1) NOT NULL DEFAULT 0,
  next_task_no BIGINT NOT NULL DEFAULT 1,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL
);

CREATE TABLE jade_team_tasks (
  root_run_id VARCHAR(80) NOT NULL,
  task_id VARCHAR(40) NOT NULL,
  revision BIGINT NOT NULL,
  subject VARCHAR(200) NOT NULL,
  description MEDIUMTEXT NOT NULL,
  status VARCHAR(24) NOT NULL,
  owner_id VARCHAR(80) NULL,
  owner_name VARCHAR(80) NULL,
  blocked_by_json TEXT NOT NULL,
  write_scopes_json TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  PRIMARY KEY(root_run_id, task_id),
  KEY idx_team_status(root_run_id, status)
);
```

落地在 [`TeamMapper.java`](https://github.com/jasoncai0/Jade/blob/main/src/main/java/io/github/jasoncai0/agent/team/TeamMapper.java)。

`TeamStores.create(MysqlSettings)` 连接失败会回退到内存 store 并写日志。这个 fallback 让本地 demo 仍能启动，但验收要看 `team` 的持久化行为，不能假设 MySQL 生效。

### 3.3 CAS 不是可选项

Lead 和多个 teammate 都可能改同一个任务。Jade 用 `expectedRevision` 做乐观锁：

```java
TeamTask saved = store.save(root, task, next);
if (saved == null) {
    throw new IllegalStateException("team task revision changed; refresh the board");
}
```

MySQL 第二道保护：

```sql
UPDATE jade_team_tasks
SET revision = #{next.revision}, ...
WHERE root_run_id = #{rootRunId}
  AND task_id = #{taskId}
  AND revision = #{expected.revision}
```

更新 0 行就是 stale。没有 CAS，两个成员同时 claim 会把 owner 互相覆盖。

## 4. 任务生命周期

{{< fig "diagrams/02-team-task-lifecycle.svg" "任务状态、依赖、CAS 和删除 tombstone。" >}}

状态机保持最小：

1. `PENDING → IN_PROGRESS`：claim；所有 blocker 必须 completed。
2. `IN_PROGRESS → COMPLETED`：complete；owner 或 Lead。
3. `COMPLETED → IN_PROGRESS`：reopen；owner 或 Lead。
4. 任意非删除 → `DELETED`：Lead 或 owner；被依赖的任务不能删。
5. `DELETED` 是 tombstone，不再出现在默认列表。

`writeScopes` 只做 UI 提示。两个任务写同一路径不会互相阻塞——这是 advisory，不是文件锁。

## 5. 可写 teammate 和普通 subagent 的差别

Jade 原来的 `delegate_task` 是同步、只读、单层、最多 2 个并发：父代理等结果，子代理只有 `list_files/read_file/search_text/load_skill/reflect`。它适合“帮我看一下”，不适合团队。

Team teammate 是另一种运行形态：

| | 普通 subagent | Team teammate |
|---|---|---|
| 生命周期 | 一次调用，结束关闭 | inbox 驱动，可继续 |
| 工具 | 只读白名单 | 全部 built-ins（除 `ask_user`）+ team tools |
| 写文件 | 不允许 | 允许，进入审批 |
| 下游 | 不能再委派 | 可 `send_message`、`wait_agent` |
| 数量 | 每父 2、depth 1 | 无 depth/member 上限 |
| 结果 | 一次性 summary | `lastResult` + 事件流/inbox |

关键代码在 [`AgentService.toolsForRun`](https://github.com/jasoncai0/Jade/blob/main/src/main/java/io/github/jasoncai0/agent/agent/AgentService.java#L424-L444) 和 [`TeamService.spawn`](https://github.com/jasoncai0/Jade/blob/main/src/main/java/io/github/jasoncai0/agent/team/TeamService.java#L218-L237)。

```java
if (team.isMember(run)) {
    ToolRegistry registry = new ToolRegistry();
    for (Tool tool : BuiltInTools.all()) {
        if (!tool.spec().name().equals("ask_user")) {
            registry.register(tool);
        }
    }
    TeamToolset.register(registry, team, run);
    return registry;
}
```

teammate 故意不拿 `ask_user`：多个成员同时向用户弹卡会失控。需要决策时用 `send_message` 回 Lead，由 Lead 决定是否向用户提问。

## 6. 权限回传

每个 teammate 有自己的 `PermissionBroker`。`write_file`、`run_command` 触发后的路径是：

```text
teammate broker → TeamService.pendingPermissions(root)
                → GET /api/runs/{root}/team
                → Lead: allow once / allow for session / deny
                → POST /api/runs/{root}/team/permissions/{id}
                → teammate broker resolves
```

[`TeamService.pendingPermissions`](https://github.com/jasoncai0/Jade/blob/main/src/main/java/io/github/jasoncai0/agent/team/TeamService.java#L289-L301) 遍历运行时成员，从 child 的 broker 取 `pendingFor(runId)`。这里有一个容易写错的点：参数是 **run id**，不是 request id；一开始传错，Team drawer 会永远空。

Team 工具本身在 `PermissionPolicy.READ_TOOLS` 里自动放行；真正的文件/命令动作仍走四档权限。Lead 的“allow for session”只作用于该 child session，不会提升到全局。

## 7. 一次完整时序

{{< fig "diagrams/03-team-sequence.svg" "Lead、TeamService、worker、MySQL 的任务与审批时序。" >}}

图里把步骤压成了 11 条，实际代码里对应三块：

1. `team_task_create` / `team_task_update` 走 CAS 和依赖校验；
2. `team_spawn` 创建 child、入队初始 task、启动虚拟线程；
3. `memberLoop` 消费 inbox，`runTeamTurn` 跑可写 turn，`send_message` 回 Lead。

## 8. 核心代码

### 8.1 spawn

```java
public TeamMember spawn(AgentRun actor, String requestedName, String task) {
    String root = requireEnabledRoot(actor);
    if (task == null || task.isBlank()) throw new IllegalArgumentException("task is required");
    String name = uniqueName(root, requestedName);
    AgentRun child = agents.newTeamRun(actor, root, name, task);
    TeamMember member = new TeamMember(child.id(), name, root, actor.id(), child);
    membersById.put(child.id(), member);
    membersByRoot.computeIfAbsent(root, ignored -> new CopyOnWriteArrayList<>()).add(member);
    member.inbox().offer(new TeamMember.InboxMessage(task, false));
    executor.submit(() -> memberLoop(member));
    return member;
}
```

[`TeamService.java`](https://github.com/jasoncai0/Jade/blob/main/src/main/java/io/github/jasoncai0/agent/team/TeamService.java#L218-L237)。`newTeamRun` 用 `Workspace.open(parent.workspace().root())` 指向同一目录，不是复制。

### 8.2 memberLoop

```java
private void memberLoop(TeamMember member) {
    while (!closed && !member.closed()) {
        TeamMember.InboxMessage message = member.inbox().take();
        member.running(true);
        member.currentTask(message.text());
        member.lastError(null);
        try {
            RunSnapshot snapshot = agents.runTeamTurn(member, message.text(), message.continuation());
            member.lastResult(snapshot.finalAnswer() == null
                    ? snapshot.status() : snapshot.finalAnswer());
        } catch (Throwable t) {
            member.lastError(t.getMessage());
        } finally {
            member.running(false);
            synchronized (member.monitor()) {
                member.monitor().notifyAll();
            }
        }
    }
}
```

[`TeamService.java`](https://github.com/jasoncai0/Jade/blob/main/src/main/java/io/github/jasoncai0/agent/team/TeamService.java#L329-L356)。`runTeamTurn` 会 `beginTurn`、注入 Team policy、跳过 Goal review、跳过长期记忆写入，最后 `finishTurn` 但**不关闭** event log——这是成员能继续接收消息的前提。

### 8.3 按角色装配工具

```java
private ToolRegistry toolsForRun(AgentRun run) {
    if (team.isMember(run)) {
        ToolRegistry registry = new ToolRegistry();
        for (Tool tool : BuiltInTools.all()) {
            if (!tool.spec().name().equals("ask_user")) {
                registry.register(tool);
            }
        }
        TeamToolset.register(registry, team, run);
        return registry;
    }
    if (run.state().isSubagent()) return subagentTools();
    ToolRegistry registry = new ToolRegistry().registerAll(tools.all());
    registry.register(new DelegateTaskTool(subagents(), run));
    if (team.enabledFor(run)) TeamToolset.register(registry, team, run);
    return registry;
}
```

[`AgentService.java`](https://github.com/jasoncai0/Jade/blob/main/src/main/java/io/github/jasoncai0/agent/agent/AgentService.java#L424-L444)。

### 8.4 wait_agent 超时不是任务失败

`wait_agent` 默认只等 600 秒。等待窗口结束不代表 teammate 失败，更不代表任务该被重派。旧输出只有一句 `Timed out.`，Lead 模型很容易把它当成“成员卡死”，再 `team_spawn` 一个替代成员，造成两份工作写同一个 workspace。

现在返回值先分组，再给每个成员状态：

```text
Timed out after 600s while some members were still active.
This is an observation timeout, not a task failure.
Still running: worker
Queued: writer
Completed: auditor
Failed: -

Do not reassign or spawn a duplicate for still-running/queued members.
Inspect list_agents and the task board, then send_message or wait again.
```

同时：

- `timedOut=true` 只表示观察窗口到期，不是 `RunStatus`；
- `stillRunning` / `queued` 是活跃成员，不能重派；
- `completed` / `failed` 是已经 idle 的成员，按最后结果判断；
- `idle` 是没有结果的空闲成员，需要进一步询问；
- 工具仍返回 `ok`，不会把 run 标成失败。

这个边界必须由 Lead 的调度策略处理：超时后先看 `list_agents` 和任务板，再决定继续等、发消息，还是显式 reopen/reassign。不要用 timeout 直接触发重复分派。

## 9. API 和 UI

```text
GET  /api/runs/{id}/team
POST /api/runs/{id}/team                    {enabled:true|false}
GET  /api/runs/{id}/team/tasks/{taskId}
POST /api/runs/{id}/team/permissions/{requestId}
     {decision:"allow"|"deny", scope:"once"|"session"}
```

[`WebServer.java`](https://github.com/jasoncai0/Jade/blob/main/src/main/java/io/github/jasoncai0/agent/web/WebServer.java#L249-L274) 里，`/team` 的 toggle 只匹配 `segments.length == 2`，否则 `/team/tasks/...` 会被 toggle handler 抢走。这是实际踩过的路由优先级坑。

`GET /api/runs` 过滤 `team-*` / `sub-*`，避免内部 child 污染 Session 栏；直接 GET 单个 child 仍可用，Team drawer 仍能看到成员。

UI 是只读 board：

- enable/disable toggle；
- members：running/idle、currentTask、lastResult、lastError；
- tasks：id/revision/status/owner/subject/description；
- pending child permissions：Deny / Allow once / Allow for session。

和 DSH 一样，任务创建/修改由模型工具完成，UI 不提供编辑按钮。UI 只做观察和审批。

## 10. 验收

```bash
cd java-coding-agent
./run.sh --check
./run.sh --port 8081
```

实际验收证据：

1. `task-1 Audit PENDING revision 1` 在服务重启后仍在；
2. worker 返回 `child-done`，`send_message` 把 `worker-done` 送回 Lead；
3. writer 的 `write_file` 产生 `perm-8a5bf30b`，Lead allow once 后 child 回复 `child-wrote`；
4. `TeamTaskStoreTest` 3 个、`TeamServiceTest` 1 个、`WebServerTest.teamApiTogglesSessionBoard` 1 个；
5. 全量 `mvn test`：162 tests，0 failures。

## 11. 限制

1. **单进程。** 所有成员共享 JVM、workspace 和宿主工具链；不是容器/VM 隔离。
2. **advisory writeScopes。** 不锁文件，两个 teammate 可以同时写同一路径；冲突由 Lead 最终 verification 发现。
3. **冷成员不恢复。** MySQL 恢复任务和 enabled，但成员是运行时对象；重启后需要 Lead 重新 spawn。
4. **无自动 owner 释放。** teammate 崩溃后任务仍显示 IN_PROGRESS；需要 Lead reopen/reassign。
5. **任务变更不唤醒成员。** 只有 `send_message` 会推进 idle 成员。
6. **Team 关闭不等于删除。** 任务和成员历史保留，工具/入口隐藏。
7. **默认 off。** `AGENT_TEAM_ENABLED=false`；当前测试 root 显式开启。

## 12. 复盘

这轮实现里最容易低估的不是任务表，而是三个运行时细节：

1. **成员必须可继续。** 一次性 child 无法接收 `send_message`，任务板会变成摆设。
2. **权限必须跨 run 路由。** 子 run 的审批请求如果只留在自己的 broker，Lead 看不到，任务会卡死。
3. **冷成员必须承认。** 把成员恢复成“看起来还活着”比不恢复更危险；Jade 选择保留任务、丢掉成员，并让 UI 显示 0 members。

Team 不是让 Agent 变多的按钮。它是一套把“分工”变成可检查状态、把“写入”变成可审批动作、把“最终正确”留给 Lead 的协议。
