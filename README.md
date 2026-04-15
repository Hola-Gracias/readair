# astrbot_plugin_readair

> 读空气 + 当前消息防抖 + 原始历史存储 + 会话内串行回复（V1）

AstrBot 插件，基于 v4.23.1 公开接口实现。在不改写平台适配器的前提下，全部逻辑落在插件层完成。

---

## V1 能力范围

### 做什么

- **读空气**：trigger 关键词 → 概率采样 → AI 判定 三级触发；AI 判定可单独选择 chat provider。
- **当前消息防抖**：同一 UMO、同一用户、窗口期内连发的消息会被聚合为一条"当前消息"再送进回复链路，避免对话被打断。
- **原始历史存储**：按真实到达顺序存主历史账本，按 `UMO` 隔离，落盘为 JSON。
- **会话内串行回复**：同 UMO 内回复按触发序串行；不同 UMO 并行。
- **完全接管回复权**：已进入主链路的消息，插件判定不回复后也不再放行 AstrBot 本体默认 LLM，避免双回复。
- **历史 / 当前消息严格分离**：历史块只写触发前的内容；当前消息不混入历史块。

### 不做什么（V1 非目标）

- 不修改平台适配器
- 不实现通用平台碎片消息重组
- 不做中文分词 / 拼音 / 模糊匹配
- 不实现独立 reply_model
- 不实现流式回复、多段消息链、Reply 锚点
- 不实现 OCR / ASR / 图像理解（但预留了扩展槽位）
- 不实现全量消息防抖（只做"当前消息防抖"）
- 不做 stale cancellation

以上能力在 V1 架构中已留出清晰扩展位置，后续版本可增量接入。

---

## 安装

把本插件目录放到 AstrBot 的 `data/plugins/astrbot_plugin_readair/` 下，或通过 WebUI 的"插件市场 → 从 zip 安装"上传 zip。

AstrBot 版本要求：**v4.5.7+**（用到了 `get_current_chat_provider_id` / `llm_generate`）。推荐 v4.23.1+。

依赖：无（纯标准库）。

---

## 目录结构

```
astrbot_plugin_readair/
├── metadata.yaml
├── _conf_schema.json        # WebUI 可视化配置
├── main.py                  # Star 入口，编排五层
├── README.md
└── core/
    ├── __init__.py
    ├── models.py            # NormalizedInboundMessage / CandidateTask / DebounceWindow
    ├── normalizer.py        # Layer 1 接入标准化层
    ├── gatekeeper.py        # Layer 2 命中判定层
    ├── debouncer.py         # Layer 3 防抖聚合层
    ├── storage.py           # Layer 4 上下文 / 存储层
    ├── executor.py          # Layer 5 回复执行层
    └── history_render.py    # 注入版 History 渲染器
```

---

## 运行时数据

主历史账本存在：

```
data/plugin_data/astrbot_plugin_readair/history/{platform_id}/group/{group_id}.json
data/plugin_data/astrbot_plugin_readair/history/{platform_id}/private/{user_id}.json
```

文件结构（schema v2）：

```json
{
  "version": 2,
  "platform": "aiocqhttp",
  "chat_type": "group",
  "session_id": "123456",
  "records": [
    {"type": "user_message", "ts": 1700000000.0, "ingest_seq": 1, "sender_id": "...", "text": "..."},
    {"type": "assistant_message", "ts": 1700000003.0, "text": "...", "task_id": "task-xxxx"}
  ]
}
```

超过 `storage_max_count` 时从**头部**移除最旧记录。

---

## 配置项说明

所有配置项均可在 WebUI 的"插件配置"页修改；修改后下次创建任务时即生效（运行中的任务使用的是创建时冻结的配置快照）。

### A. 总体准入

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable_switch` | bool | `true` | 回复主链路总开关。关闭后不创建任务、不接管、不 stop_event；**但若按存储策略本该写入历史，仍会落盘**。关闭读空气 ≠ 关闭记账。 |
| `group_list_mode` | `blacklist` / `whitelist` | `blacklist` | 群聊准入模式 |
| `group_list` | list[str] | `[]` | 群号列表（语义由 mode 决定） |
| `private_list_mode` | `blacklist` / `whitelist` | **`whitelist`** | 私聊准入模式（默认 whitelist + 空 → 默认不参与读空气） |
| `private_list` | list[str] | `[]` | 用户 ID 列表 |

> **注意**：
> - `private_list` 只控制私聊**能否进入回复链路**；私聊是否写入历史由 `enable_private_chat_storage` 独立控制。二者互不替代。
> - **V2 默认策略变更**：`private_list_mode` 默认 `whitelist` + 空列表 + `enable_private_chat_storage=false`，即**默认私聊既不回复也不落盘**。如需让私聊参与读空气，把目标用户加入 `private_list`；如需让私聊落盘，显式把 `enable_private_chat_storage` 设为 true。

### B. 命中判定

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `group_user_blacklist` | list[str] | `[]` | 用户黑名单（命中不触发回复，但仍按规则写入历史） |
| `trigger_keywords` | list[str] | `[]` | 触发关键词（子串匹配） |
| `block_keywords` | list[str] | `[]` | 屏蔽关键词（命中不触发回复；附着消息命中则不附着） |
| `response_probability` | float | `0.0` | 未命中 trigger 时的概率采样（0.0–1.0） |
| `ai_judge_provider` | string | `""` | 读空气 AI 判定使用的 **chat provider ID**；留空 = 关闭 AI 判定，概率通过即进入主回复 |
| `ai_judge_prompt` | text | 见默认值 | AI 判定的 system prompt，要求模型只输出 `REPLY` 或 `SKIP` |

> `ai_judge_provider` 必须选 chat provider，不要选 TTS / STT / Embedding。插件运行时会做类型校验并在校验失败时按 `SKIP` 保守处理。

**判定顺序硬约束**：
```
空消息 → 会话准入 → 用户黑名单 → 屏蔽词 → 触发词 → 概率 → AI 判定
```

### C. 当前消息防抖

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable_debounce` | bool | `true` | 启用当前消息防抖窗口 |
| `debounce_min_wait_seconds` | float | `1.5` | 最小等待；每条附着消息重置该计时器 |
| `debounce_max_wait_seconds` | float | `6.0` | 硬性上限；到时间强制关窗 |
| `debounce_concurrent_limit` | int | `4` | 单个 UMO 内允许同时存在的活跃窗口数上限 |

> `debounce_concurrent_limit` 是**单 UMO 内**上限，不是全局上限。

### D. 历史存储

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `storage_max_count` | int | `400` | 主账本单文件最多保留条数 |
| `context_message_limit` | int | `40` | 注入版 History 最多携带的最近记录条数 |
| `enable_private_chat_storage` | bool | **`false`** | 私聊是否写入主历史账本（默认关闭，避免私聊被无差别落盘） |
| `store_tool_result_in_history` | bool | `false` | 工具调用结果是否入账本（V1 预留，本版未启用写入路径） |
| `store_tool_call_in_history` | bool | `false` | 工具调用事件是否入账本（V1 预留） |
| `store_proactive_sent_message_in_history` | bool | `true` | 主动发送的消息是否入账本（V1 预留） |
| `store_task_created_in_history` | bool | `false` | 任务创建事件是否入账本（调试用） |
| `store_task_summary_in_history` | bool | `false` | 任务 summary 是否入账本 |

> `store_*` 系列开关在 V1 预留，对应 `storage.append_generic()`；上层链路挂点会在后续版本接入。

- `At` 参与关键词匹配：标准化时把 `[at]` 追加进 `match_text`；若 At 目标为 bot 自己，再追加 `[at_bot]`。Reply 不进入 `match_text`（V1 扩展槽位）。
- 空消息判定优先看 `event.get_message_outline()`：outline 非空 → 必不空；outline 空 + 无有效语义组件（image/record/voice/video/file/face/at）+ raw_text 空 → 判空直接丢弃，覆盖 Napcat 私聊"对方正在输入"这类状态型伪消息。

---

## 五层架构

```
AstrMessageEvent
    │
    ▼
[Layer 1] normalizer        → NormalizedInboundMessage（冻结）
    │
    ▼
[Layer 3] 尝试附着           → 若同 UMO+sender 有活跃窗口，附着后写原始历史，结束
    │
    ▼
[Layer 2] gatekeeper        → 空 / 准入 / 用户黑名单 / 屏蔽词 / 触发词 / 概率
    │
    ├── not_taken_over        : 不 stop，不存储，交给本体
    ├── taken_over_drop       : 写原始历史，stop_event
    └── create_task (kw / prob):
            │
            ▼
[Layer 4] snapshot_and_append → 冻结快照（不含当前触发消息）+ 写当前原始消息
            │
            ▼
   后台调度（stop_event 立即返回）
            │
            ▼
[Layer 3] open_window → wait_and_close
            │
            ▼
[Layer 5] executor.execute
            ├── (probability_triggered) AI 判定 → REPLY / SKIP
            ├── 主回复 llm_generate（沿用 get_current_chat_provider_id）
            ├── context.send_message
            └── append_assistant（仅发送成功后）
```

**固定顺序约束**（设计文档 八）：

1. 任务创建
2. 读取历史快照（不含当前触发消息）
3. 当前原始消息写入主历史账本
4. 决定是否建立当前消息防抖窗口
5. 窗口关闭
6. AI 判定（仅概率任务）
7. 主回复
8. 发送成功后写入 `assistant_message`

**并发与一致性**：

- 同 UMO 内"读取快照 → 追加原始记录 → 更新窗口 / 任务状态"处于同一串行临界区（`HistoryStore` 按账本文件维度的 `asyncio.Lock`）。
- 同 UMO 内回复执行通过 `UMOSequencer`（per-UMO FIFO + `asyncio.Condition`）**严格按 `trigger_seq` 顺序**执行；后触发的任务即使先完成防抖也必须等待前一个序号完成。
- 不同 UMO 之间完全并行。

---

## 完全接管回复权

- 仅在 **会话已准入并由本插件接管** 时调用 `event.stop_event()`。
- 会话未准入的消息不接管，保留 AstrBot 本体默认 LLM 路径。
- 已接管后：无论是否回复都阻断默认 LLM，避免双回复（设计文档 十五.1）。

---

## 扩展槽位（V1 不实现，已预留）

对应设计文档 十六：

| 扩展 | 位置 | 已预留 |
| --- | --- | --- |
| OCR | `normalizer._build_display_text` / `NormalizedInboundMessage.image_ocr_text` | ✅ 字段 + 管线占位 |
| ASR | `normalizer` / `NormalizedInboundMessage.record_asr_text` | ✅ 字段 |
| 图像理解 | `NormalizedInboundMessage.image_caption_text` / `match_text` 构建 | ✅ 字段 |
| 流式回复 | `executor._run_main_reply` 返回分支 | ⚠️ 待接入（目前单条文本） |
| 多段消息链 | `executor` 发送分支 | ⚠️ 待接入 |
| Reply 锚点 | `executor` 发送器 | ⚠️ 待接入 |
| 独立 reply_model | `executor._run_main_reply` 获取 provider_id 处 | ⚠️ 待接入 |
| Agent Runner 兼容 | `executor._run_main_reply` | ✅ **V2 主路径已切到 `tool_loop_agent`**；V3 进一步从 `context.get_llm_tool_manager()` 收集当前会话 tools/subagents/handoff 传入，**不再显式传 `ToolSet([])` 清空能力**；不可用时 fallback 到 `llm_generate` |
| 上下文压缩 | `storage` + `history_render` | ⚠️ 待接入 |
| 调试 / 审计 | `CandidateTask.debug_trace` / 各层 logger | ✅ 基础已在 |

---

## 卸载 / 重载

插件 `terminate()` 会依次：

1. `DebouncerManager.force_close_all()` 强制关闭所有活跃窗口；
2. `Executor.cancel_all()` 取消所有在途回复任务；
3. `HistoryStore.flush_all()` 把内存缓存的账本刷盘。

对应设计文档 十五.5。

---

## 已知风险与未来工作

- **provider 类型校验**：`ai_judge_provider` 仅做朴素字符串匹配 `chat / chat_completion / llm`，不同版本 AstrBot 的 `Provider.provider_type` 字段枚举可能不同；校验失败时保守按 `SKIP` 处理。
- **消息去重**：V1 不对平台重试导致的同 `message_id` 重复投递做去重；需要时可在 `normalizer` 加一圈 recent_ids LRU。
- **主回复依赖 `tool_loop_agent`**：V2 主路径使用 `context.tool_loop_agent(event=..., tools=ToolSet([]))`。如果运行的 AstrBot 版本没有暴露 `ToolSet` 或 `tool_loop_agent`，会自动 fallback 到 `llm_generate`。

---

## 相关文档

- AstrBot 插件开发文档（中）：<https://docs.astrbot.app/dev/star/plugin-new.html>
- AstrBot Provider 使用：`docs/agent/providers.md`
- AstrBot 钩子与事件流：`docs/plugin_config/hooks.md`、`docs/design_standards/event_flow.md`

---

## License

MIT（见 `LICENSE`）。
