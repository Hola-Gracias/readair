"""V1 数据模型定义。

严格对齐《V1 插件设计方案》第九/十/十一节：
- NormalizedInboundMessage：接入标准化入站对象（冻结）
- CandidateTask：候选回复任务
- DebounceWindow：当前消息防抖窗口
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 接入标准化入站对象
# ---------------------------------------------------------------------------
@dataclass
class NormalizedInboundMessage:
    """冻结后的入站标准化对象。

    三种文本视图的语义分离是为后续 OCR / ASR / 图像理解 留出演进空间：
    - raw_text：平台可见的原始纯文本。
    - display_text：用于展示 / History 渲染的文本（可注入 [图片] [语音] 等标记，或 OCR 结果）。
    - match_text：用于关键词 / 屏蔽词 / 触发词命中判定的文本。
    """

    # --- 身份字段 ---
    umo: str
    platform_id: str
    chat_type: str                # "group" | "private"
    session_id: str
    group_id: str | None
    sender_id: str
    sender_name: str
    self_id: str
    message_id: str

    # --- 时间字段 ---
    platform_ts: float            # 平台声明的时间戳
    ingest_ts: float              # 插件接入时的本地时钟
    ingest_seq: int               # 插件接入顺序号（单调递增，作为真实到达顺序权威来源）

    # --- 文本视图 ---
    raw_text: str
    display_text: str
    match_text: str
    message_outline: str

    # --- 组件字段 ---
    raw_component_types: list[str] = field(default_factory=list)

    # --- 原生标志位 ---
    is_private_chat: bool = False
    is_wake: bool = False
    is_at_or_wake_command: bool = False
    call_llm: bool = False
    is_admin: bool = False

    # --- 接入判定 ---
    is_empty: bool = False
    drop_reason: str | None = None

    # --- 扩展槽位（V1 不实现，字段保留） ---
    image_ocr_text: str | None = None
    record_asr_text: str | None = None
    image_caption_text: str | None = None

    # --- 调试 ---
    raw_message_digest: str | None = None


# ---------------------------------------------------------------------------
# 候选回复任务
# ---------------------------------------------------------------------------
@dataclass
class CandidateTask:
    """已进入防抖聚合层的候选任务。"""

    task_id: str
    umo: str
    task_type: str                # "keyword_triggered" | "probability_triggered"
    trigger_seq: int              # 全局单调递增，用作同 UMO 内串行回复的序号
    trigger_ts: float
    snapshot_ts: float

    # 冻结快照
    config_snapshot: dict[str, Any]
    history_snapshot: list[dict[str, Any]]

    # 身份信息
    sender_id: str
    sender_name: str
    platform_id: str
    chat_type: str
    session_id: str
    group_id: str | None

    # 当前消息（防抖关窗后回填）
    current_message: NormalizedInboundMessage | None = None
    current_message_text: str = ""
    upgraded_from_probability: bool = False

    debug_trace: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 当前消息防抖窗口
# ---------------------------------------------------------------------------
@dataclass
class DebounceWindow:
    """单用户单窗口。仅附着同一 UMO + 同一 sender_id + 窗口期内的后续消息。"""

    window_id: str
    umo: str
    sender_id: str
    task_id: str
    task_type: str

    open_ts: float
    last_msg_ts: float
    close_at: float               # 基于 min/max wait 计算出的关窗截止

    attached_event_ids: list[str] = field(default_factory=list)
    attached_messages: list[NormalizedInboundMessage] = field(default_factory=list)

    version: int = 0              # 每次附着 +1，用于判定窗口是否仍有效
    upgraded_from_probability: bool = False
    closed: bool = False
