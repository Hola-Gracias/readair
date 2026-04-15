"""Layer 3 — 防抖聚合层。

职责：
- 仅对"已经创建候选回复任务"的消息建立当前消息窗口。
- 只吸收同一 UMO、同一 sender_id、窗口期内的后续消息。
- 维护"单用户单窗口"；同一 UMO 允许多个不同用户并存（上限由 debounce_concurrent_limit 控制）。
- 允许 probability_triggered 因后续附着消息命中 trigger 升级为 keyword_triggered。
- 关窗后产出最终当前消息。

不负责：
- 修改主历史账本顺序
- 回溯吞并旧消息
- 跨会话聚合
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from .gatekeeper import hit_trigger
from .models import DebounceWindow, NormalizedInboundMessage

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger("readair.debouncer")


class DebouncerManager:
    """防抖窗口管理器。每个 (umo, sender_id) 最多一个活跃窗口。"""

    def __init__(self) -> None:
        # (umo, sender_id) -> DebounceWindow
        self._windows: dict[tuple[str, str], DebounceWindow] = {}
        # umo -> 活跃窗口计数
        self._per_umo_count: dict[str, int] = {}
        self._lock = asyncio.Lock()

    # ---------- 查询 ----------
    def get_window(self, umo: str, sender_id: str) -> DebounceWindow | None:
        return self._windows.get((umo, sender_id))

    def active_count_for_umo(self, umo: str) -> int:
        return self._per_umo_count.get(umo, 0)

    # ---------- 开窗 ----------
    async def open_window(
        self,
        msg: NormalizedInboundMessage,
        task_id: str,
        task_type: str,
        *,
        min_wait: float,
        max_wait: float,
        concurrent_limit: int,
    ) -> DebounceWindow | None:
        """为触发消息打开一个新窗口；超上限则返回 None（由上层决定降级为 no-debounce 或拒绝）。"""
        async with self._lock:
            key = (msg.umo, msg.sender_id)
            if key in self._windows:
                # 防御：该 sender 已有窗口。正常流程不应到达这里（附着应走 attach）。
                return None
            if self._per_umo_count.get(msg.umo, 0) >= concurrent_limit:
                return None
            now = time.time()
            win = DebounceWindow(
                window_id=f"win-{uuid.uuid4().hex[:8]}",
                umo=msg.umo,
                sender_id=msg.sender_id,
                task_id=task_id,
                task_type=task_type,
                open_ts=now,
                last_msg_ts=now,
                close_at=min(now + max_wait, now + min_wait),  # 初始先按 min；每次附着重置
                attached_event_ids=[msg.message_id] if msg.message_id else [],
                attached_messages=[msg],
                version=1,
                upgraded_from_probability=False,
            )
            # 对 max_wait 做硬上限标记
            win._hard_deadline = win.open_ts + max_wait  # type: ignore[attr-defined]
            self._windows[key] = win
            self._per_umo_count[msg.umo] = self._per_umo_count.get(msg.umo, 0) + 1
            return win

    # ---------- 附着 ----------
    async def try_attach(
        self,
        msg: NormalizedInboundMessage,
        cfg: dict[str, Any],
        *,
        min_wait: float,
        max_wait: float,
    ) -> tuple[DebounceWindow | None, str]:
        """尝试把 msg 附着到 (umo, sender_id) 的活跃窗口。

        返回 (window, action):
            action == "attached"           成功附着
            action == "upgraded_attached"  附着 + 概率升级为 keyword
            action == "no_window"          该 sender 没有活跃窗口
            action == "closed"             窗口存在但已关闭
        """
        async with self._lock:
            key = (msg.umo, msg.sender_id)
            win = self._windows.get(key)
            if win is None:
                return None, "no_window"
            if win.closed:
                return None, "closed"

            # 附着
            now = time.time()
            win.last_msg_ts = now
            win.attached_messages.append(msg)
            if msg.message_id:
                win.attached_event_ids.append(msg.message_id)
            win.version += 1

            # 关窗点重置为 max(hard_deadline, now + min_wait) 的下限
            hard = getattr(win, "_hard_deadline", win.open_ts + max_wait)
            win.close_at = min(now + min_wait, hard)

            # 尝试概率 -> 关键词 升级
            action = "attached"
            if win.task_type == "probability_triggered":
                if hit_trigger(msg.match_text, cfg) is not None:
                    win.task_type = "keyword_triggered"
                    win.upgraded_from_probability = True
                    action = "upgraded_attached"
            return win, action

    # ---------- 关窗 ----------
    async def wait_and_close(self, win: DebounceWindow) -> DebounceWindow:
        """等待窗口自然关闭。轮询 last_msg_ts 与 hard_deadline。

        注意：这里不持锁长时间等待；每次 sleep 后重新读取窗口状态。
        """
        hard = getattr(win, "_hard_deadline", win.open_ts + 6.0)
        while True:
            now = time.time()
            if now >= hard:
                break
            # close_at 可能被 attach 不断推后
            async with self._lock:
                current_close_at = win.close_at
                current_hard = getattr(win, "_hard_deadline", hard)
            if now >= current_close_at:
                break
            sleep_for = min(current_close_at, current_hard) - now
            if sleep_for <= 0:
                break
            # 粒度最多 0.5s；让 attach 有机会及时推迟关窗
            await asyncio.sleep(min(sleep_for, 0.5))

        # 关窗
        async with self._lock:
            win.closed = True
            # 解除 sender 绑定，让后续消息进入新的触发判定路径
            key = (win.umo, win.sender_id)
            if self._windows.get(key) is win:
                del self._windows[key]
                self._per_umo_count[win.umo] = max(0, self._per_umo_count.get(win.umo, 1) - 1)
        return win

    async def force_close_all(self) -> list[DebounceWindow]:
        """插件卸载时使用：强制关闭所有活跃窗口并返回它们。"""
        async with self._lock:
            wins = list(self._windows.values())
            for w in wins:
                w.closed = True
            self._windows.clear()
            self._per_umo_count.clear()
            return wins


def build_current_message_text(win: DebounceWindow) -> str:
    """把窗口内所有附着消息按真实到达顺序拼成 "当前消息" 文本。"""
    parts: list[str] = []
    for m in sorted(win.attached_messages, key=lambda x: x.ingest_seq):
        t = m.display_text.strip()
        if t:
            parts.append(t)
    return "\n".join(parts)
