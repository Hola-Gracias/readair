"""Layer 1 — 接入标准化层。

只做一件事：把 AstrMessageEvent 冻结成 NormalizedInboundMessage。

不做：会话准入、关键词命中、防抖、历史、回复。

V2 变更（定向修复）：
- match_text 现在会把 At 纳入匹配源：
    - 出现 At 组件 → 追加 `[at]`
    - At 目标为 self_id → 额外追加 `[at_bot]`
  Reply 按设计保留为 V1 扩展槽位，不进入 match_text。
- 空消息判定改为优先看 message_outline：
    - outline 非空 → 必不空
    - outline 为空 + 有"有效语义组件"（image/record/voice/video/file/face/at）→ 也非空
    - 其余（如 Napcat 私聊"对方正在输入"这类纯状态伪消息）→ 判空
"""

from __future__ import annotations

import hashlib
import itertools
import time
from typing import TYPE_CHECKING

from .models import NormalizedInboundMessage

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


# 全局单调递增的接入序号。真实到达顺序的权威来源（不单纯依赖 platform_ts）。
_ingest_seq_counter = itertools.count(1)


# 允许作为"有效语义"承载的组件类型关键词（小写匹配）。
# Plain 本身的语义已经在 raw_text 中体现，不在此列。
# Reply 是 V1 预留扩展槽位，不计入"有效语义"。
_MEANINGFUL_COMPONENT_KEYWORDS = ("image", "record", "voice", "video", "file", "face", "at")


def _safe_str(value) -> str:
    return "" if value is None else str(value)


def _component_type_name(comp) -> str:
    """尽量稳定地拿组件类型名。"""
    try:
        t = getattr(comp, "type", None)
        if t:
            return str(t)
    except Exception:
        pass
    return type(comp).__name__


def _at_target_id(comp) -> str | None:
    """从 At 组件中抽取目标 ID；兼容不同平台字段命名差异。"""
    for attr in ("qq", "user_id", "target_id", "target"):
        v = getattr(comp, attr, None)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _is_at_component(type_name: str) -> bool:
    """判定是否为 At 组件（不含 AtAll）。"""
    lower = type_name.lower()
    # 兼容 "At" / "at" / 带命名空间 "xxx.At"
    if lower == "at":
        return True
    if lower.endswith(".at"):
        return True
    return False


def _is_atall_component(type_name: str) -> bool:
    lower = type_name.lower()
    return lower in ("atall", "at_all") or lower.endswith(".atall")


def _has_meaningful_component(component_types: list[str]) -> bool:
    """是否存在任何"有效语义"组件（纯图片/语音/视频/文件/表情/At 等）。"""
    for t in component_types:
        lower = t.lower()
        if any(k in lower for k in _MEANINGFUL_COMPONENT_KEYWORDS):
            return True
    return False


def _build_display_text(raw_text: str, component_types: list[str]) -> str:
    """display_text：人类可读展示，给 History 渲染用。

    OCR / ASR / 图像理解 接入后，只需扩展这里（以及 NormalizedInboundMessage 里
    image_ocr_text 等字段），不破坏原始存储模型。详见设计文档 十三.4。
    """
    placeholders: list[str] = []
    for t in component_types:
        lower = t.lower()
        if _is_at_component(t):
            placeholders.append("[@]")
        elif _is_atall_component(t):
            placeholders.append("[@全体]")
        elif "image" in lower:
            placeholders.append("[图片]")
        elif "record" in lower or "voice" in lower:
            placeholders.append("[语音]")
        elif "video" in lower:
            placeholders.append("[视频]")
        elif "file" in lower:
            placeholders.append("[文件]")
        elif "face" in lower:
            placeholders.append("[表情]")
        elif "reply" in lower:
            # Reply 锚点属 V1 扩展槽位，这里只补占位
            placeholders.append("[回复]")

    base = raw_text or ""
    if placeholders and base:
        return base + " " + " ".join(placeholders)
    if placeholders:
        return " ".join(placeholders)
    return base


def _build_match_text(raw_text: str, components: list, self_id: str) -> str:
    """match_text：用于关键词 / 屏蔽词 / 触发词命中判定。

    V2：把 At 纳入匹配源。出现 At 追加 `[at]`，At 目标为 bot 自己则再追加 `[at_bot]`。
    Reply 不进入 match_text（V1 扩展槽位）。
    未来 OCR 接入后可在这里追加 image_ocr_text 片段。
    """
    tokens: list[str] = []
    seen_at_bot = False
    seen_at = False
    self_id_str = _safe_str(self_id)

    for comp in components:
        tname = _component_type_name(comp)
        if _is_at_component(tname):
            target = _at_target_id(comp)
            if not seen_at:
                tokens.append("[at]")
                seen_at = True
            if (
                not seen_at_bot
                and self_id_str
                and target is not None
                and target == self_id_str
            ):
                tokens.append("[at_bot]")
                seen_at_bot = True

    if not tokens:
        return raw_text
    if not raw_text:
        return " ".join(tokens)
    return raw_text + " " + " ".join(tokens)


def _decide_is_empty(
    *, outline: str, raw_text: str, component_types: list[str]
) -> bool:
    """V2 空消息判定：优先看 message_outline。

    规则：
    - outline strip 后非空 → 非空。
    - outline strip 为空 + 无有效语义组件 + raw_text 也为空 → 空。
    - 其它情况（有任何有效语义组件或 raw_text）→ 非空。

    Napcat 私聊"对方正在输入"这类伪消息通常 outline 空 + 无有效组件 + raw_text 空
    → 判空直接丢弃。
    """
    if outline and outline.strip():
        return False
    if _has_meaningful_component(component_types):
        return False
    if raw_text and raw_text.strip():
        return False
    return True


def normalize(event: "AstrMessageEvent") -> NormalizedInboundMessage:
    """把事件对象冻结为入站标准化对象。

    空消息不在这里丢弃，只打上 is_empty 标记，由上层决定是否放行。
    """
    ingest_seq = next(_ingest_seq_counter)
    ingest_ts = time.time()

    msg_obj = event.message_obj
    raw_text = _safe_str(getattr(msg_obj, "message_str", None)) or _safe_str(
        getattr(event, "message_str", "")
    )

    # 组件列表
    try:
        components = event.get_messages() or []
    except Exception:
        components = []
    component_types = [_component_type_name(c) for c in components]

    # UMO / 会话
    umo = event.unified_msg_origin
    try:
        platform_id = event.get_platform_id()
    except Exception:
        platform_id = _safe_str(
            getattr(event, "platform_meta", None) and event.platform_meta.id
        )

    is_private = False
    try:
        is_private = event.is_private_chat()
    except Exception:
        pass
    chat_type = "private" if is_private else "group"

    self_id = _safe_str(event.get_self_id())

    # 时间戳
    platform_ts = float(getattr(msg_obj, "timestamp", 0) or 0) or ingest_ts

    # 摘要字段（空判定的首选依据）
    try:
        outline = event.get_message_outline()
    except Exception:
        outline = raw_text[:80] if raw_text else ""
    outline = _safe_str(outline)

    # 三种文本视图
    display_text = _build_display_text(raw_text, component_types)
    match_text = _build_match_text(raw_text, components, self_id)

    # 原生标志位（失败则默认 False）
    def _flag(name: str) -> bool:
        v = getattr(event, name, None)
        if callable(v):
            try:
                return bool(v())
            except Exception:
                return False
        return bool(v) if v is not None else False

    is_wake = _flag("is_wake_up") or _flag("is_wake")
    is_at_or_wake_command = _flag("is_at_or_wake_command")
    call_llm = _flag("call_llm")
    is_admin = _flag("is_admin")

    # V2：空消息判定优先看 outline
    is_empty = _decide_is_empty(
        outline=outline, raw_text=raw_text, component_types=component_types
    )
    drop_reason = None
    if is_empty:
        if not outline.strip():
            drop_reason = "empty_outline_no_meaningful_component"
        else:
            drop_reason = "empty"

    # 调试摘要
    digest_src = f"{umo}|{getattr(msg_obj, 'message_id', '')}|{raw_text}"
    digest = hashlib.md5(digest_src.encode("utf-8", errors="ignore")).hexdigest()[:12]

    return NormalizedInboundMessage(
        umo=umo,
        platform_id=platform_id,
        chat_type=chat_type,
        session_id=_safe_str(event.get_session_id()),
        group_id=_safe_str(event.get_group_id()) or None,
        sender_id=_safe_str(event.get_sender_id()),
        sender_name=_safe_str(event.get_sender_name()),
        self_id=self_id,
        message_id=_safe_str(getattr(msg_obj, "message_id", "")),
        platform_ts=platform_ts,
        ingest_ts=ingest_ts,
        ingest_seq=ingest_seq,
        raw_text=raw_text,
        display_text=display_text,
        match_text=match_text,
        message_outline=outline,
        raw_component_types=component_types,
        is_private_chat=is_private,
        is_wake=is_wake,
        is_at_or_wake_command=is_at_or_wake_command,
        call_llm=call_llm,
        is_admin=is_admin,
        is_empty=is_empty,
        drop_reason=drop_reason,
        raw_message_digest=digest,
    )
