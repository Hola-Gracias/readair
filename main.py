"""ReadAir V1 插件入口。

五层装配：
    AstrMessageEvent
        └─> normalize()                     [Layer 1]
        └─> (若命中已有窗口 → attach)        [Layer 3]
        └─> gatekeeper.decide()              [Layer 2]
        └─> storage.snapshot_and_append()    [Layer 4]   创建任务 + 冻结快照
        └─> schedule bg task:
                debouncer.open/wait_close()  [Layer 3]
                executor.execute()           [Layer 5]   AI 判定 + 主回复 + 发送 + 写 assistant

硬约束（设计文档 八）：
    任务创建 → 读取历史快照（不含当前触发消息）→ 当前原始消息写入主历史账本
    → 建立防抖窗口 → 窗口关闭 → (概率任务) AI 判定 → 主回复 → 发送成功后写 assistant_message

回复权接管：
    插件只在 "会话已准入并由本插件接管" 的前提下调用 event.stop_event()；
    非准入消息不接管，保留 AstrBot 本体默认 LLM 路径。
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core import gatekeeper
from .core.debouncer import DebouncerManager, build_current_message_text
from .core.executor import Executor
from .core.models import CandidateTask
from .core.normalizer import normalize
from .core.storage import HistoryStore


@register(
    "astrbot_plugin_readair",
    "YourName",
    "读空气 + 当前消息防抖 + 原始历史存储 + 会话内串行回复（V1）",
    "1.0.0",
    "https://github.com/YourName/astrbot_plugin_readair",
)
class ReadAirPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self._rng = random.Random()
        # 全局单调递增的 trigger_seq，用于日志与未来可扩展的严格顺序队列
        self._trigger_counter = 0

        # 主历史账本根目录：data/plugin_data/{plugin_name}/history/
        base_dir = self._resolve_history_base_dir()
        self._history = HistoryStore(base_dir)

        self._debouncer = DebouncerManager()
        self._executor = Executor(context, self._history)

        logger.info(f"[readair] plugin instantiated, history base dir: {base_dir}")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        logger.info("[readair] initialized.")

    async def terminate(self) -> None:
        """卸载 / 重载：先 flush 活跃窗口，再清理队列和缓存。

        对应设计文档 十五.5：插件卸载 / 重载时，必须先 flush 所有活跃窗口，
        再清理队列和临时状态。
        """
        logger.info("[readair] terminating: flush active windows...")
        try:
            wins = await self._debouncer.force_close_all()
            logger.info(f"[readair] force-closed {len(wins)} active debounce windows")
        except Exception as e:
            logger.warning(f"[readair] force_close_all failed: {e}")

        logger.info("[readair] cancelling inflight reply tasks...")
        try:
            await self._executor.cancel_all()
        except Exception as e:
            logger.warning(f"[readair] cancel_all failed: {e}")

        logger.info("[readair] flushing history ledgers...")
        try:
            await self._history.flush_all()
        except Exception as e:
            logger.warning(f"[readair] history flush failed: {e}")

        logger.info("[readair] terminate done.")

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #
    def _resolve_history_base_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            return get_astrbot_data_path() / "plugin_data" / self.name / "history"
        except Exception:
            # 兜底：工作目录下 data/ 相对路径
            return Path("data") / "plugin_data" / self.name / "history"

    def _snapshot_config(self) -> dict[str, Any]:
        """把当前 WebUI 配置冻结为一次任务的 config_snapshot。"""
        try:
            return dict(self.config)
        except Exception:
            # AstrBotConfig 在某些版本下不直接实现 dict 协议；兜底转 json 再 load
            import json
            try:
                return json.loads(json.dumps(self.config, default=lambda o: getattr(o, "__dict__", str(o))))
            except Exception:
                return {}

    def _next_trigger_seq(self) -> int:
        self._trigger_counter += 1
        return self._trigger_counter

    # ------------------------------------------------------------------ #
    # 核心事件 handler：单一入口接管所有消息
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        try:
            await self._handle(event)
        except Exception as e:
            # 兜底：不让 handler 异常把整条事件链搞崩
            logger.error(f"[readair] handler error: {e}", exc_info=True)

    async def _handle(self, event: AstrMessageEvent) -> None:
        # ---- Layer 1: 标准化 ----
        msg = normalize(event)
        if msg.is_empty:
            # 空消息直接丢弃；不接管、不存储、不 stop，交给本体处理
            return

        cfg = self._snapshot_config()

        # ---- Layer 3 前置：尝试附着到现有窗口（同 umo + sender_id） ----
        existing = self._debouncer.get_window(msg.umo, msg.sender_id)
        if existing is not None and not existing.closed:
            # 会话准入是前置条件，既有窗口意味着之前已准入，这里不再重复判定
            # 附着前先检查屏蔽词（设计文档 七.14：附着消息先做屏蔽词再判定）
            block = gatekeeper._block_hit(msg.match_text, cfg)
            if block is not None:
                # 命中屏蔽词：不附着，但仍写入原始历史
                await self._append_raw(msg, cfg)
                event.stop_event()
                return

            win, action = await self._debouncer.try_attach(
                msg, cfg,
                min_wait=float(cfg.get("debounce_min_wait_seconds", 1.5)),
                max_wait=float(cfg.get("debounce_max_wait_seconds", 6.0)),
            )
            if win is not None:
                await self._append_raw(msg, cfg)
                logger.debug(
                    f"[readair] attached to window {win.window_id} "
                    f"(action={action}, task_type={win.task_type})"
                )
                event.stop_event()
                return
            # 窗口已关闭或 race；继续走正常判定

        # ---- Layer 2: 命中判定 ----
        decision = gatekeeper.decide(msg, cfg, self._rng)
        logger.debug(f"[readair] gate decision: {decision.verdict} ({decision.reason})")

        if decision.verdict == gatekeeper.ADMIT_NOT_TAKEN_OVER:
            # 不接管 + 不存储 → 不 stop，让本体继续处理
            return

        if decision.verdict == gatekeeper.ADMIT_STORAGE_ONLY:
            # enable_switch=off 但会话已准入：
            # 仍按存储策略写原始历史，但不接管、不 stop、不创建任务
            await self._append_raw(msg, cfg)
            return

        if not decision.create_task:
            # 已接管但不回复（屏蔽词 / 用户黑名单 / 概率失败）
            # 仍按规则写入原始历史，避免空洞（设计文档 四.5）
            await self._append_raw(msg, cfg)
            event.stop_event()
            return

        # ---- Layer 4: 冻结历史快照 + 写入原始消息（串行临界区） ----
        try:
            snapshot = await self._history.snapshot_and_append_user(
                msg,
                context_message_limit=int(cfg.get("context_message_limit", 40)),
                storage_max_count=int(cfg.get("storage_max_count", 400)),
                enable_private_chat_storage=bool(cfg.get("enable_private_chat_storage", True)),
                write=True,
            )
        except Exception as e:
            logger.error(f"[readair] snapshot_and_append failed: {e}", exc_info=True)
            event.stop_event()
            return

        # ---- 构造候选任务 ----
        task_type = (
            "keyword_triggered"
            if decision.verdict == gatekeeper.ADMIT_CREATE_KEYWORD
            else "probability_triggered"
        )
        task = CandidateTask(
            task_id=f"task-{uuid.uuid4().hex[:8]}",
            umo=msg.umo,
            task_type=task_type,
            trigger_seq=self._next_trigger_seq(),
            trigger_ts=msg.ingest_ts,
            snapshot_ts=time.time(),
            config_snapshot=cfg,
            history_snapshot=snapshot,
            sender_id=msg.sender_id,
            sender_name=msg.sender_name,
            platform_id=msg.platform_id,
            chat_type=msg.chat_type,
            session_id=msg.session_id,
            group_id=msg.group_id,
            current_message=msg,
            # 防抖关窗前先填初值；关窗后会被 build_current_message_text 覆盖
            current_message_text=msg.display_text,
        )
        task.debug_trace.append(f"created:{decision.reason}")

        # 关键同步段：分配 trigger_seq -> 登记到 per-UMO 顺序队列 -> stop_event
        # 这三步之间不出现 await，保证同一 UMO 的登记顺序严格等于 trigger_seq 顺序。
        self._executor.register_task(task)

        # 接管回复权：阻断本体默认 LLM
        event.stop_event()

        # ---- 调度后台任务（Layer 3 防抖 + Layer 5 执行） ----
        # 统一通过 _task_lifecycle 调度，保证无论哪条分支、是否抛异常，
        # sequencer.finish 都会在 finally 中被调用，不会卡住后续 trigger_seq。
        use_debounce = bool(cfg.get("enable_debounce", True))
        if not use_debounce:
            task.debug_trace.append("debounce:disabled")
        self._executor.schedule(self._task_lifecycle(task, event, use_debounce=use_debounce))

    # ------------------------------------------------------------------ #
    # 任务生命周期包装：防抖 + 执行 + 兜底释放 sequencer
    # ------------------------------------------------------------------ #
    async def _task_lifecycle(
        self, task: CandidateTask, event: AstrMessageEvent, *, use_debounce: bool
    ) -> None:
        """统一的任务生命周期：防抖 -> 执行；保证 sequencer 一定被释放。

        说明：
        - ``Executor.execute`` 内部已有 try/finally 释放 sequencer；这里外层再放一层
          try/except + release 只是防御性兜底，覆盖"防抖阶段就异常/被取消、根本
          没进到 execute" 的路径。``sequencer.finish`` 是幂等的，重复调用安全。
        """
        try:
            if use_debounce:
                await self._run_with_debounce(task, event)
            else:
                # 不启用防抖 → 当前消息 = 触发消息本身，直接执行
                await self._executor.execute(task, event)
        except asyncio.CancelledError:
            # terminate / 超时取消：兜底释放
            try:
                await self._executor.release(task)
            except Exception:
                pass
            raise
        except Exception as e:
            logger.error(
                f"[readair] task lifecycle error (task={task.task_id}): {e}",
                exc_info=True,
            )
            try:
                await self._executor.release(task)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 防抖 + 执行：后台任务
    # ------------------------------------------------------------------ #
    async def _run_with_debounce(self, task: CandidateTask, event: AstrMessageEvent) -> None:
        cfg = task.config_snapshot

        win = await self._debouncer.open_window(
            task.current_message,
            task.task_id,
            task.task_type,
            min_wait=float(cfg.get("debounce_min_wait_seconds", 1.5)),
            max_wait=float(cfg.get("debounce_max_wait_seconds", 6.0)),
            concurrent_limit=int(cfg.get("debounce_concurrent_limit", 4)),
        )
        if win is None:
            # 超单 UMO 并发上限 → 降级为 no-debounce（用触发消息本身作为当前消息）
            task.debug_trace.append("debounce:concurrent_limit_reached")
            await self._executor.execute(task, event)
            return

        try:
            await self._debouncer.wait_and_close(win)
        except Exception as e:
            logger.warning(f"[readair] wait_and_close failed: {e}")
            # 即便 wait 失败也尽量执行一次
            try:
                await self._debouncer.force_close_all()
            except Exception:
                pass

        # 关窗后回填最终当前消息
        task.current_message_text = build_current_message_text(win) or task.current_message.display_text
        if win.upgraded_from_probability and task.task_type == "probability_triggered":
            task.task_type = "keyword_triggered"
            task.upgraded_from_probability = True
            task.debug_trace.append("upgraded_from_probability")

        await self._executor.execute(task, event)

    # ------------------------------------------------------------------ #
    # 原始消息落盘（附着、命中屏蔽词、概率失败等场景共用）
    # ------------------------------------------------------------------ #
    async def _append_raw(self, msg, cfg: dict[str, Any]) -> None:
        try:
            await self._history.append_user_only(
                msg,
                storage_max_count=int(cfg.get("storage_max_count", 400)),
                enable_private_chat_storage=bool(cfg.get("enable_private_chat_storage", True)),
            )
        except Exception as e:
            logger.warning(f"[readair] append raw failed: {e}")
