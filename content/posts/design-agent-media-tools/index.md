---
title: "设计 Agent 的媒体工具：视频与图片生成是怎么接进来的"
date: "2026-11-24T18:35:00+08:00"
summary: "一句「把这张图做成 5 秒竖版视频」，工具层要装配参数、提交任务、轮询、取回、写进资产。拆 OpenAI Agents SDK、Google ADK、Dify、Coze、MCP 和 Hermes 六种接法：动态 schema、三种执行位置、base64 与 artifact 的取舍、submit-poll-download 时序，以及网络、凭证、文件系统、内容四条边界。"
description: "设计类 Agent 的 image/video 生成工具实现：工具 schema 如何按 provider 能力和模型目录生成、托管/进程内/插件三种执行位置、base64 与 artifact 的取舍、长任务轮询与超时、首尾帧与积分成本、URL 与凭证与沙箱与内容四条边界。"
tags: ["agent", "architecture", "harness"]
slug: "design-agent-media-tools"
---

设计类 Agent 的媒体工具，难点不在写 prompt。

一个品牌任务里，用户说「把这张产品图做成 5 秒竖版，首帧用它，尾帧用发布会主视觉」。工具层要回答四个问题：当前模型支不支持尾帧？9:16 在不在枚举里？5 秒要花多少积分？生成的文件放哪，下一轮模型还能不能看见？这四个问题分别落在 schema、执行位置、返回值和资产层上，和提示词关系不大。

下面按一次调用的顺序拆：五段流水线、schema 怎么装配、三种执行位置、返回值怎么选、长任务怎么等、设计场景的参数与成本，最后是四条边界和一份实现清单。

## 1. 一次调用要跨五段

{{< fig "diagrams/01-pipeline.svg" "五个阶段与三种执行位置：托管工具把 2–4 段留在厂商侧，进程内 provider 自己轮询，插件/网关把任务交给平台；模型只看见装配和落地。">}}

装配是把 agent 的意图变成 provider 能接受的 payload：prompt、比例、时长、首帧或参考图。提交是建 job，拿回一个 id。等待是轮询或等回调。取回是下载并校验。落地是写资产，把引用交回去。

Hermes 在 `video_generate` 的描述里写得很直白：一次生成可能 30 秒到几分钟，调用会阻塞到就绪，结果在 `video` 字段，可能是 HTTP URL，也可能是绝对文件路径。这句话同时约束模型和实现：模型要知道这是慢操作，实现要自己消化等待。

五段里只有第 1 段和第 5 段对模型可见。第 2–4 段在工具内部完成，谁跑这三段，密钥就在谁手里，产物就落在谁那边。后面三种执行位置的区别，全部来自这个分工。

## 2. 工具 schema 是算出来的

静态 schema 的 `required` 只有 `prompt`，可选属性不止一个：`video_generate` 还有 `duration`、`aspect_ratio`、`resolution`，`image_generate` 还有 `aspect_ratio`。运行时再按 provider 的 `capabilities()` 和模型条目加：`image_url`、`reference_image_urls` 两者都可能出现；`negative_prompt`、`audio`、`seed` 只在 video 的动态 schema 里，`upscale` 和 provider 声明的 creative controls 只在 image 里。

```python
properties = {"prompt": static_props["prompt"]}
if "image" in effective_modalities:
    properties["image_url"] = {"type": "string"}           # 省略 description
    if caps.get("max_reference_images", 0) > 0:
        properties["reference_image_urls"] = {"type": "array"}

for flag, name, schema in _CAPABILITY_PARAMS:
    if caps.get(flag):
        properties[name] = schema
```

`duration` 的 min/max 读模型条目，`aspect_ratio` 和 `resolution` 的 enum 收紧到 provider 声明的后端能力集合，不一定等于当前模型条目。这样做的收益有三个：

1. 模型不会调用不存在的参数。工具描述里没有 `audio`，模型就不会为了「加个背景音」乱传一个字段。
2. 错误在 schema 层被挡住。provider 不必为一个不支持的组合返回 400，模型也不用读一遍错误再重试。
3. 换模型不用改 agent 的提示词。能力表在配置变更时重建，`model_tools.get_tool_definitions()` 按 config.yaml 的 mtime 失效缓存。

{{< fig "diagrams/02-schema.svg" "同一份工具代码，按 provider 能力和模型目录装配出不同的运行时 schema；模型不支持的能力既不出现在参数里，也不会因为历史调用里带着就报错。">}}

没出现在 schema 里的参数仍然接受。原因写在 Hermes 的注释里：replay compat。模型可能复制上一轮的调用，而上一轮用的后端支持某个参数；如果这一轮直接 400，整段对话就卡住了。handler 会把未声明的控制项过滤掉，再交给 provider。

这两句不矛盾：schema 决定模型第一次看到什么，handler 的宽容决定历史调用能不能过。前者收紧，后者放行已知旧字段，两者都在 provider 之前完成。

`model` 不在 schema 里。Hermes 的注释是「the backend/model is user configuration (`video_gen.model`), never an agent choice」。这条规则把模型选择从对话里拿出来，放到产品配置里。换模型只需要改两行配置，agent 的提示词和画布逻辑都不动。

对不支持的值，两种处理方式都合理，取决于参数是连续量还是离散动作。OpenRouter 插件用 `_nearest()` 把 duration 按距离、resolution 按像素高度、aspect_ratio 按数值比钳到最近的支持值；Hermes 对 `operation` 和 `video_url` 直接拒绝，因为 edit/extend 在不同 provider 之间语义不一致，统一表面只会制造歧义。钳位适合时长、比例这类连续量，拒绝适合动作。

OpenAI 的托管工具走的是另一条路。`ImageGenerationTool` 的 schema 由厂商固定，配置项有 `action`、`size`、`quality`、`background`、`output_format`、`partial_images`、`input_fidelity`，还有 `model`、`moderation`、`input_image_mask`、`output_compression`；它只能在 `OpenAIResponsesModel` 上用。托管工具没有 schema 协商，能力边界就是厂商 API 的边界。

## 3. 三种执行位置

托管工具把第 2–4 段留在厂商侧。OpenAI 的 `image_generation_call` 是一个工具结果 item，结构大致是：

```json
{"id": "ig_123", "type": "image_generation_call", "status": "completed",
 "revised_prompt": "...", "result": "<base64>"}
```

这是托管工具的返回，base64 在 item 里，接入方不用自己下载。自己写的 function tool 不走这条路径，返回值该用什么类型，下一节讲。多轮编辑靠 `previous_response_id` 或把上一轮的 item 带回来；`action` 设成 `edit` 却没有图像上下文时，请求会报错。`partial_images` 可以设 0–3，生成过程中通过 `response.image_generation_call.partial_image` 事件推预览。密钥、重试、存储都在厂商侧，接入方只负责把 item 放进对话。

进程内 provider 把这三段拿回自己手里。Hermes 定义了一个 `VideoGenProvider` 抽象类，默认 `capabilities()` 是 text-only，能力集合为 8 个常见比例、5 档分辨率，默认值 16:9、720p，时长 1–10 秒，所有可选能力为 false。具体后端实现 `generate()`，返回统一的 dict：`success`、`video`、`model`、`prompt`、`modality`、`aspect_ratio`、`duration`、`provider`，失败时加 `error` 和 `error_type`。`OpenAICompatibleVideoGenProvider` 走 `client.videos` 的 create → poll → download；OpenRouter 插件自己实现 POST、轮询和下载。

图片侧同理。Hermes 的 `image_generate` 走 FAL 队列，提交时带 `x-idempotency-key`，等待用 daemon 线程加 `is_interrupted()` 轮询，用户中断后工具停止等待，远端 job 继续跑。产物只写一个文件，另外给宿主路径和 agent 可见路径两套视图，供沙箱内的后端读取。

插件和网关把执行交给平台。Dify 的插件跑在插件进程里，产物由平台落盘：YAML 里声明 provider、tool_configurations 和 tool_parameters，返回时调 `create_image_message(url)`，平台自动下载并展示；需要原始文件时用 `create_blob_message(bytes, meta)`，meta 里写 `mime_type`。Coze 把视频生成做成工作流节点，参数收敛成模型、比例、分辨率、时长几档。MCP 则是把 provider 包成 server，ADK 用 `MCPToolset` 接进来。

| 接法 | 谁跑提交/轮询/下载 | 工具返回 | 代价 |
|---|---|---|---|
| OpenAI 托管 `ImageGenerationTool` | OpenAI | `image_generation_call`（base64） | 只能在 Responses 模型上用，参数由厂商定 |
| Hermes 进程内 provider | 工具进程 | 视频 URL 或本地路径 | 自己实现轮询、下载、清理 |
| Dify 插件 | 插件进程，平台落盘 | URL 或 blob | 受插件沙箱和平台文件策略限制 |
| Coze 工作流节点 | Coze | 节点输出 URL | 参数被产品收敛，只有固定几档 |
| MCP server（ADK） | MCP server | text、image、resource_link | 没有 video 类型，得自己约定 artifact |

怎么选，按三条判断：

1. 模型只能用 Responses，且不打算自建资产层：选托管。轮询、下载、存储都不用写。
2. 要接多个 provider，产物要进自己的资产树和审核流程：选进程内。代价是下载、mime 校验、清理、重试都得自己实现。
3. 已经在 Dify/Coze 里，或团队没有对象存储：选插件/网关，让平台托管文件。不要为了「统一」再包一层进程内 provider。

接新 provider 的最小顺序：

1. 实现 `capabilities()`，按真实能力填。默认值故意 fail closed，填错会让 schema 少参数。
2. 实现 `generate()`，返回统一 dict。未知 kwargs 忽略，不要把不支持的控制项透传给 API。
3. 注册到 registry，配置 `video_gen.provider` 和模型。没配置时，只有一个可用 provider 才会自动选。
4. 用 t2v 和 i2v 各跑一次，检查 schema 里出现的参数、终态、下载路径；再断网跑一次，确认错误类型不是成功。

## 4. 返回值：像素不进上下文

工具结果会进入对话历史，并在后续每一轮请求里重发。一个 10 MB 的 mp4 转成 base64 大约 13 MB 文本，按 token 算就是几百万；模型还没开始想下一句，上下文已经满了。

ADK 的多模态 codelab 在这里踩过一次，报错原文是「the tool returned results directly in the form of base64 string too, that will exceed the maximum token」。它的修法是写进 artifact，只把 artifact id 返回给模型，没有截断。

{{< fig "diagrams/03-return-shape.svg" "返回形态的分岔：小图可以内联，大图和视频必须落 artifact；视频没有 MCP content type，只能 resource_link 或 blob。">}}

常见的返回形态有四种：

1. **URL**：provider 的临时地址，或者对象存储的签名地址。最轻，但有有效期，也可能要求鉴权。
2. **base64 内联**：直接放在工具结果里。只适合小图预览，且只应活一轮。
3. **file_id / artifact id**：文件已经在平台的存储里，模型按 id 再取。Agents SDK 里，自己写的 function tool 用 `ToolOutputImage`，要 `image_url` 或 `file_id`；`ToolOutputFileContent` 要 `file_data`、`file_url`、`file_id` 三者之一。
4. **bytes + mime**：MCP 的 embedded resource 或 Dify 的 blob。适合跨进程传递，但要自己管大小和生命周期。

这些 id 不通用：`file_id` 是 OpenAI 文件系统的 id，artifact id 是 ADK session 里的文件名，asset id 是自己资产层的 id，MCP 的 `resource_link` 只是一层 URL 包装。生命周期分别归平台、session、资产层和 provider；跨层传递要换 id，不能假设能互换。embedded resource 算第 4 种（bytes + mime），resource_link 算第 1 种（URL）的包装。

ADK 的 `after_tool_callback` 是第 3 种形态的典型写法：

```python
result = json.loads(tool_response.content[0].text)
video_bytes = base64.b64decode(result["video_data"])
await tool_context.save_artifact(
    filename=f"video_{function_call_id}.mp4",
    artifact=Part(inline_data={"mime_type": "video/mp4", "data": video_bytes}),
)
result.pop("video_data")
result["video_artifact_id"] = filename
```

工具返回从 10 MB 降到几十字节。模型下一轮要复查时，再按 id 把 artifact 取出来。

Hermes 的 `save_url_video` 做的是同一件事的另一半：provider 给的 URL 有时效，工具在 180 秒内下载，限制 200 MB，只认 mp4、webm、mov、mkv，落到 `$HERMES_HOME/cache/generated/videos/`。让工具下载而不是让模型去 fetch，原因是模型没有 provider 的凭据，也不该为临时 URL 再发一次请求。

MCP 这里有一个缺口：2025-06-18 的 tools 规范里，content 只有 text、image、audio、resource_link 和 embedded resource，没有 video。视频只能塞进 resource_link（URL、name、mimeType、size），或者当 blob 传 base64，或者在文本里写 URL。接 MCP 的时候要自己约定用哪一种。

## 5. 长任务：submit、poll、download

{{< fig "diagrams/04-job-timeline.svg" "一次同步调用的内部时序：提交拿 job id，按间隔轮询到终态，再下载、校验、写资产；超时和中断都走单独的返回路径。">}}

Hermes 的 `OpenAICompatibleVideoGenProvider` 用 5 秒间隔、900 秒硬截止。它不用 SDK 自带的 `create_and_poll`，注释写得很清楚：「polls ~1/s forever on a non-terminal status, pinning the tool-executor thread」。一个卡住的 job 会占住工具执行线程，后面的调用全排队。OpenRouter 插件用 10 秒间隔、900 秒截止，终态集合是 `completed`、`failed`、`cancelled`、`canceled`、`expired`，比 Hermes 多 `expired`，少了 `succeeded` 和 `error`。ADK 的 Veo 例子是 5 秒一次 `operations.get`。

轮询之外还有三件事。

**幂等键**。FAL 的提交带 `x-idempotency-key`，值是 uuid4，但每次提交都新建一个；429 重试的注释写明 resubmit ONCE under a new key。所以幂等键只在同一次 HTTP 请求重发时复用，工具级重试不能保证不重复建 job、不重复扣费。LlamaIndex 的 WaveSpeed 工具更保守：提交 POST 从不重试，因为客户端不知道 job 到底建没建，重试可能生成两条。

**终态集合**。Hermes 认 `completed`、`succeeded`、`failed`、`error`、`cancelled`、`canceled`；OpenRouter 用 `completed`、`failed`、`cancelled`、`canceled`、`expired`。只有成功终态才下载，失败终态直接返回 `error_type`。把 `cancelled` 当失败还是当用户取消，要在产品层分开，前者可以重试，后者不该。

**中断**。Hermes 的等待在 daemon 线程里跑，主线程按切片轮询 `is_interrupted()`；用户中断后工具停止等待，远端 job 继续跑，结果可能还能从 job id 取回。LlamaIndex 的工具支持按 prediction id 恢复长任务，也是同一个思路：连接可以断，任务不该丢。

超时后 job 可能还在跑。工具应该返回 job id 和当前状态，而不是把 job 当成失败丢掉；下一次可以用 id 接着查，或者让用户决定要不要等。

## 6. 设计场景：参数与成本

通用视频模型有几十个参数，设计工具只暴露几个：首帧、尾帧、参考图、比例、时长、分辨率、seed。Coze 的 Seedance 节点把帧率固定为 24fps，时长 3–12 秒（pro）或 4–12 秒（1.5 pro），分辨率到 1080p；5 秒/10 秒、480p/720p 是常见的计费档位。lite 必须设尾帧，pro 只支持首帧。选模型时，这条差异比参数数量更重要，因为首尾帧直接决定构图和落版。

首帧定起手构图，尾帧定最后一帧，参考图定风格或主体。三者都要求图片先进入 provider 能访问的位置：公网 HTTPS URL，或者 `data:image/` 的内联数据。Hermes 的 `_confine_source_images()` 在非 local 终端后端会把本地图片路径转成 data URL，否则 provider 看不到沙箱里的文件。

成本要前置。Lovart 的视频页把价格写在生成按钮前：静帧 3 积分，10 秒 480p 的 Seedance 2.0 Mini 40 积分，Seedance 2.5 280 积分，250 积分 9.90 美元起。设计 Agent 一次任务要出几个候选，成本会乘上候选数；不显示单次价格，用户没法判断「再来一版」的代价。

批量是设计场景的常态。一个 campaign 要 6 个尺寸、每个 3 个候选，就是 18 次生成。Coze 用循环节点批量提交，工具层要配合并发上限和失败重试；串行等 18 次，一次任务要跑十几分钟。

prompt 改写也要记录。OpenAI 的 `revised_prompt` 会告诉你模型实际用了什么描述；ADK 的 codelab 会在调用前把镜头、光线、风格补进 prompt。改写结果不回写，同一个 prompt 两次结果差异就无法解释，评测也复现不了。

## 7. 四条边界

媒体工具比文本工具多四个出错面：网络、凭证、文件系统、内容策略。

| 边界 | 典型故障 | 实现动作 |
|---|---|---|
| 网络 | 图片 URL 指向内网；provider 拉不到沙箱文件 | 只接受 `https://` 或 `data:image/`；本地文件先转 data URL；域名白名单 |
| 凭证 | 下载 URL 是响应里的第三方地址，bearer key 跟着发过去 | 下载地址从配置的 origin 推导；不信任响应里的 `unsigned_urls` |
| 文件系统 | 宿主路径在容器里不存在；路径映射错，产物读不到 | 同时给 `host_image` 和 `agent_visible_image` 两套路径视图；按后端做路径映射 |
| 内容 | 政策拒绝被当成网络失败，反复重试 | 区分 `policy` 和 `transport` 错误；记录 `safety_identifier`、水印、C2PA |

网络这一条最容易被低估。模型给一个 `http://127.0.0.1:8080/a.png`，工具如果直接转发给 provider，就是一次 SSRF；provider 在内网，可能读到不该读的东西。OpenRouter 插件的做法是只接受 `https://` 或 `data:image/`，让 provider 自己去取公网图；Hermes 在非 local 后端先把本地图转成 data URL，避免把宿主路径发出去。

凭证这一条出过事：provider 的响应里带 `unsigned_urls`，看起来像下载地址，但 host 不受控。OpenRouter 插件只用配置里的 `base_url` 拼 `/videos/{id}/content`，并设置 `trusted_origin=True`，避免 bearer key 被发到别人的域名。

文件系统这一条和沙箱有关。FAL 的托管网关请求体大约 4.5 MB 就会 413，实测 4.0 MB 的 base64 能过、4.5 MB 失败，所以内联源图的预算定在 3.5 MB。超过预算的图要先压缩或走对象存储，不能硬塞。

内容策略要前置到工具层。生成失败时，`error_type` 要能区分政策拒绝、参数错误、网络超时；政策拒绝重试没有意义，参数错误要告诉模型改哪个字段，超时才可以重试或续查。

## 8. 实现清单

1. **schema 只放产品需要的参数**：prompt 必填，其余按能力加；`model` 留在配置里，不进对话。
2. **能力来自 provider 和 catalog**：schema 在配置变更时重建；未广告的参数仍然接受，避免回放 400。
3. **提交带幂等键**：POST 不盲目重试；超时后先用 job id 查，再决定要不要重提。
4. **轮询有间隔和硬截止**：5–10 秒一次，900 秒截止；不要用会无限轮询的 SDK helper。
5. **终态集合写全**：completed、succeeded、failed、error、cancelled、canceled、expired；只有成功才下载。
6. **下载要校验**：mime、大小、时长；限制 200 MB 和 180 秒；拒绝 HTML 错误页伪装成视频。
7. **产物落资产，返回引用**：base64 只用于预览；引用带 mime、尺寸、时长、hash，方便去重和回收。
8. **路径双视图**：宿主路径和 agent 可见路径都给；沙箱内先把本地图转 data URL。
9. **凭证不出域**：下载地址从配置 origin 推导；不信任响应里的第三方 URL。
10. **成本与内容策略前置**：调用前显示价格；记录 usage；区分政策拒绝和可重试错误。

对照这四张图检查自己的实现：如果第 2–4 段有一段落在了 agent 的提示词里，那里就是接下来要补的工具层。

## 参考

- [NousResearch/hermes-agent：video_generation_tool.py、video_gen_provider.py、image_generation_tool.py](https://github.com/NousResearch/hermes-agent)
- [OpenAI Agents SDK：Tools](https://openai.github.io/openai-agents-python/tools/)
- [OpenAI：Image generation guide](https://platform.openai.com/docs/guides/image-generation)
- [Google ADK：MCP tools](https://google.github.io/adk-docs/tools/mcp-tools/)
- [MCP 2025-06-18：Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [Dify：Tool 插件定义](https://docs.dify.ai/plugins/schema-definition/tool)
- [LlamaIndex：Tools](https://docs.llamaindex.ai/en/stable/module_guides/deploying/agents/tools/)
- [Coze 开放文档](https://www.coze.com/open/docs)
- [Lovart](https://www.lovart.ai/)

代码与默认值取自 2026 年 11 月的 Hermes main 分支；模型、价格和参数以各家文档为准。
