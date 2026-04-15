"""注入版 History 渲染器（设计文档 十三）。

渲染原则：
- 历史与当前消息严格分离；不把当前消息混入 History 块。
- 有会话头部；按日期分段；同一天内按时间正序。
- 未来 OCR / ASR / 图像理解 接入后，只需升级 display_text 生成逻辑或本渲染器，
  不影响原始存储模型。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any


def _fmt_ts(ts: float) -> str:
    try:
        return _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except Exception:
        return "??:??:??"


def _fmt_date(ts: float) -> str:
    try:
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return "????-??-??"


def render_history_block(
    snapshot: list[dict[str, Any]],
    *,
    chat_type: str,
    session_id: str,
    self_name: str = "bot",
    max_records: int | None = None,
) -> str:
    """把快照渲染为注入给 LLM 的 History 文本块。

    返回空字符串表示没有历史可注入。
    """
    if not snapshot:
        return ""

    records = snapshot
    if max_records is not None and max_records > 0:
        records = records[-max_records:]

    # 按 ts 正序（快照本身已经按真实到达顺序）
    lines: list[str] = []
    header = f"【历史】({'群聊' if chat_type == 'group' else '私聊'} · {session_id})"
    lines.append(header)

    current_date: str | None = None
    for rec in records:
        ts = float(rec.get("ts", 0) or 0)
        date = _fmt_date(ts)
        if date != current_date:
            lines.append(f"-- {date} --")
            current_date = date

        rtype = rec.get("type")
        timestr = _fmt_ts(ts)

        if rtype == "user_message":
            name = rec.get("sender_name") or rec.get("sender_id") or "user"
            text = rec.get("text") or rec.get("raw_text") or ""
            lines.append(f"[{timestr}] {name}: {text}")
        elif rtype == "assistant_message":
            text = rec.get("text") or ""
            lines.append(f"[{timestr}] {self_name}: {text}")
        elif rtype == "tool_call":
            lines.append(f"[{timestr}] (tool_call) {rec.get('name', '')}({rec.get('args', '')})")
        elif rtype == "tool_result":
            lines.append(f"[{timestr}] (tool_result) {rec.get('text', '')}")
        elif rtype == "proactive_message_sent":
            lines.append(f"[{timestr}] {self_name} (主动): {rec.get('text', '')}")
        elif rtype == "task_summary":
            lines.append(f"[{timestr}] (task_summary) {rec.get('text', '')}")
        else:
            # 未知类型：朴素展示
            lines.append(f"[{timestr}] ({rtype}) {rec.get('text', '')}")

    return "\n".join(lines)


def render_current_message_block(current_text: str, sender_name: str) -> str:
    """当前消息块。与 History 分开渲染，严格隔离。"""
    return f"【当前消息】{sender_name}: {current_text}"
