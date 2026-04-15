"""Layer 5 — 回复执行层（V2）。

职责：
- 消费 "已具备最终当前消息" 的任务。
- 概率通道任务在必要时执行 AI 判定（读空气 gate）。
- 调用主回复 Agent（tool_loop_agent 优先，llm_generate 仅在前者不可用时 fallback）。
- 发送最终回复。
- 发送成功后将 assistant_message 写入主历史账本。
- 无论成功失败都释放队列（sequencer.finish 一定被调用）。

并发模型（V2 升级，满足设计文档 十四.1 "按 trigger_seq 串行"）：
- 同 UMO 内通过 UMOSequencer 严格按 trigger_seq 顺序执行；
  即使后触发任务的防抖先关窗，也必须等前一个 trigger_seq 完成后才能执行。
- 不同 UMO 之间完全并行（每 UMO 一套独立的 condition variable + FIFO 队列）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from .history_render import render_current_message_block, render_history_block
from .models import CandidateTask

try:
    from astrbot.api import logger
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger("readair.executor")
    MessageChain = None   # type: ignore
    Plain = None          # type: ignore

# 主回复首选：tool_loop_agent（沿用当前会话 persona / tools / subagents 能力环境）
try:
    from astrbot.core.agent.tool import ToolSet  # type: ignore
except Exception:  # pragma: no cover
    ToolSet = None  # 不可用时降级到 llm_generate

if TYPE_CHECKING:
    from astrbot.api.star import Context

    from .storage import HistoryStore


# AI 判定输出协议：输出 REPLY / SKIP
AI_JUDGE_REPLY = "REPLY"
AI_JUDGE_SKIP = "SKIP"


# ---------------------------------------------------------------------------
# 会话工具集收集器
# ---------------------------------------------------------------------------
def _build_session_toolset(context) -> "ToolSet | None":
    """尽量保留当前会话可用的 tools / subagents / handoff 工具。

    AstrBot v4.23.1 的 ``Context.tool_loop_agent(tools=...)`` 把传入的 ToolSet
    原样塞进 ``ProviderRequest.func_tool`` —— 它**不会**自动合并当前会话的工具集。
    因此如果我们像早期版本一样传 ``ToolSet([])``，等同于显式清空 LLM 可用的工具，
    subagent 编排注入的 ``transfer_to_*`` / HandoffTool、插件通过 ``@filter.llm_tool``
    或 ``context.add_llm_tools`` 注册的工具都会消失。

    本函数通过 ``context.get_llm_tool_manager()`` 尽可能还原一份"当前会话公开可见
    的工具集"：
    - 优先尝试 manager 上的 ``get_full_tool_set()`` / ``get_tool_set()``
      （不同版本命名可能不同），直接使用其返回值。
    - 退化到 manager.func_list：筛掉 ``active=False`` 的条目，包起来成 ToolSet。
    - 任何异常一律返回 ``None``。调用方收到 None 时应**省略** ``tools`` 参数，
      让 ``tool_loop_agent`` 走其自身默认值（也是 None），而非主动传 ``ToolSet([])``
      覆盖掉任何潜在环境。

    注意：
    - 我们拿不到 MCP tools / 沙盒 / Skills 这类由主 pipeline ``MainAgentBuildConfig``
      汇总的运行期工具，这些走的不是 FunctionToolManager.func_list；插件层能还原的
      只有函数工具层。这是 AstrBot 公共 API 的边界，不是本插件的缺陷。
    - 不做激活/停用状态的强制变更 —— 仅读取现状。
    """
    if ToolSet is None:
        return None
    try:
        mgr = context.get_llm_tool_manager()
    except Exception as e:
        logger.debug(f"[readair] get_llm_tool_manager failed: {e}")
        return None
    if mgr is None:
        return None

    # 1) 优先走 manager 自己暴露的"拿全量工具集"方法（不同版本命名不同）
    for attr in ("get_full_tool_set", "get_tool_set", "as_tool_set", "to_tool_set"):
        m = getattr(mgr, attr, None)
        if callable(m):
            try:
                ts = m()
                if ts is not None:
                    return ts
            except Exception as e:
                logger.debug(f"[readair] {attr}() failed: {e}")

    # 2) 退化：从 func_list 自行构造，仅保留 active 的工具
    func_list = getattr(mgr, "func_list", None)
    if not func_list:
        return None
    try:
        active = [t for t in func_list if getattr(t, "active", True)]
    except Exception:
        active = list(func_list)
    if not active:
        return None
    try:
        return ToolSet(list(active))
    except Exception as e:
        logger.debug(f"[readair] ToolSet(active) construction failed: {e}")
        return None


# ---------------------------------------------------------------------------
# UMOSequencer：严格按 trigger_seq 顺序的 per-UMO 门闸
# ---------------------------------------------------------------------------
class UMOSequencer:
    """Per-UMO 严格顺序门闸。

    使用方式：
    1. 任务创建时（在 handler 同步段内）调用 ``register(umo, trigger_seq)``；
       由于 register 是同步方法，它必须与 trigger_seq 的分配处于**同一条 sync
       代码段**，这样同一 UMO 的 trigger_seq 进入队列的顺序就是单调递增的。
    2. 后台任务在调用主回复前 ``await wait_turn(umo, trigger_seq)`` —— 只有当
       该 seq 处于队列头部时才放行；否则挂起在 condition variable 上。
    3. 无论任务成功 / 失败 / 被取消，都必须调用 ``finish(umo, trigger_seq)``；
       该方法**幂等**，重复调用是安全的。
    """

    def __init__(self) -> None:
        # umo -> FIFO list of registered trigger_seqs
        self._queues: dict[str, list[int]] = {}
        # umo -> condition variable
        self._cvs: dict[str, asyncio.Condition] = {}

    def _cv_for(self, umo: str) -> asyncio.Condition:
        cv = self._cvs.get(umo)
        if cv is None:
            cv = asyncio.Condition()
            self._cvs[umo] = cv
        return cv

    def register(self, umo: str, trigger_seq: int) -> None:
        """同步登记。必须与 trigger_seq 分配处于同一 sync span 内。"""
        self._cv_for(umo)  # 预建 cv
        self._queues.setdefault(umo, []).append(trigger_seq)

    async def wait_turn(self, umo: str, trigger_seq: int) -> None:
        """挂起直到 ``trigger_seq`` 位于队列头部。"""
        cv = self._cv_for(umo)
        async with cv:
            while True:
                q = self._queues.get(umo, [])
                if q and q[0] == trigger_seq:
                    return
                if trigger_seq not in q:
                    # 已被 finish 清理（重复调用或异常清理路径），直接放行
                    return
                await cv.wait()

    async def finish(self, umo: str, trigger_seq: int) -> None:
        """幂等释放。无论该 seq 是否在队列中都会 notify_all。"""
        cv = self._cv_for(umo)
        async with cv:
            q = self._queues.get(umo)
            if q:
                try:
                    q.remove(trigger_seq)
                except ValueError:
                    pass
                if not q:
                    self._queues.pop(umo, None)
            cv.notify_all()

    async def abandon_all(self) -> None:
        """卸载时清空所有队列并唤醒所有等待者。"""
        for umo, cv in list(self._cvs.items()):
            async with cv:
                self._queues[umo] = []
                cv.notify_all()
        self._queues.clear()


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------
class Executor:
    def __init__(self, context: "Context", history_store: "HistoryStore"):
        self._context = context
        self._history = history_store
        self._sequencer = UMOSequencer()
        # 所有在途的回复任务，用于 terminate 时 cancel
        self._inflight: set[asyncio.Task] = set()

    # ---------- 任务调度相关 ----------
    def schedule(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._inflight.add(task)
        task.add_done_callback(lambda t: self._inflight.discard(t))
        return task

    def register_task(self, task: CandidateTask) -> None:
        """同步登记任务到 per-UMO 顺序队列。

        必须在 handler 的同步段内调用，以保证同一 UMO 的 trigger_seq 入队
        顺序与分配顺序一致。
        """
        self._sequencer.register(task.umo, task.trigger_seq)

    async def release(self, task: CandidateTask) -> None:
        """从顺序队列中释放任务（幂等）。"""
        await self._sequencer.finish(task.umo, task.trigger_seq)

    async def cancel_all(self) -> None:
        for t in list(self._inflight):
            t.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        self._inflight.clear()
        # 兜底：所有还在 wait_turn 挂起的协程必须被唤醒
        await self._sequencer.abandon_all()

    # ---------- 执行主体 ----------
    async def execute(self, task: CandidateTask, event) -> None:
        """严格按 trigger_seq 顺序执行一个任务。

        调用方必须事先 ``register_task()``（通常在 main 的 handler 里）。
        本方法保证在 finally 中 ``sequencer.finish``，外层可放心重复调用
        ``release()``（幂等）。
        """
        cfg = task.config_snapshot
        try:
            # 等待轮到本任务
            await self._sequencer.wait_turn(task.umo, task.trigger_seq)
            try:
                await self._execute_inner(task, event, cfg)
            except asyncio.CancelledError:
                logger.info(f"[readair] task {task.task_id} cancelled")
                raise
            except Exception as e:
                logger.error(f"[readair] task {task.task_id} failed: {e}", exc_info=True)
        finally:
            await self._sequencer.finish(task.umo, task.trigger_seq)

    async def _execute_inner(self, task: CandidateTask, event, cfg: dict[str, Any]) -> None:
        # --- 1. 渲染 History + 当前消息（严格分离） ---
        history_block = render_history_block(
            task.history_snapshot,
            chat_type=task.chat_type,
            session_id=task.session_id,
            self_name="bot",
            max_records=cfg.get("context_message_limit", 40),
        )
        current_block = render_current_message_block(
            task.current_message_text or "",
            task.sender_name or "user",
        )

        # --- 2. 概率任务：如果配置了 ai_judge_provider，执行 AI 判定（继续用 llm_generate） ---
        if task.task_type == "probability_triggered":
            judge_provider_id = (cfg.get("ai_judge_provider") or "").strip()
            if judge_provider_id:
                decision = await self._run_ai_judge(
                    judge_provider_id=judge_provider_id,
                    system_prompt=cfg.get("ai_judge_prompt") or "",
                    history_block=history_block,
                    current_block=current_block,
                )
                task.debug_trace.append(f"ai_judge={decision}")
                if decision != AI_JUDGE_REPLY:
                    logger.info(f"[readair] task {task.task_id} skipped by ai_judge")
                    return
            else:
                task.debug_trace.append("ai_judge=disabled")

        # --- 3. 主回复（优先 tool_loop_agent） ---
        reply_text = await self._run_main_reply(
            event=event,
            umo=task.umo,
            history_block=history_block,
            current_block=current_block,
        )
        if not reply_text:
            logger.info(f"[readair] task {task.task_id} got empty main reply, skip send")
            return

        # --- 4. 发送 ---
        try:
            if MessageChain is not None and Plain is not None:
                chain = MessageChain(chain=[Plain(reply_text)])
                await self._context.send_message(task.umo, chain)
            else:
                # 兜底：用 event.send
                await event.send(event.plain_result(reply_text))
        except Exception as e:
            logger.error(f"[readair] send failed for task {task.task_id}: {e}", exc_info=True)
            return

        # --- 5. 成功后写入 assistant_message ---
        target_id = task.group_id if task.chat_type == "group" else task.sender_id
        await self._history.append_assistant(
            platform_id=task.platform_id,
            chat_type=task.chat_type,
            target_id=target_id or task.session_id,
            session_id=task.session_id,
            text=reply_text,
            storage_max_count=cfg.get("storage_max_count", 400),
            task_id=task.task_id,
        )

    # ---------- AI 判定（llm_generate，保持不变） ----------
    async def _run_ai_judge(
        self,
        *,
        judge_provider_id: str,
        system_prompt: str,
        history_block: str,
        current_block: str,
    ) -> str:
        """调用 AI 判定 provider。返回 'REPLY' / 'SKIP'。失败时按 SKIP 处理（保守）。"""
        # provider 类型校验：非 chat provider 时直接 SKIP 并记录
        try:
            prov = self._context.get_provider_by_id(judge_provider_id)
            if prov is None:
                logger.warning(f"[readair] ai_judge_provider not found: {judge_provider_id}")
                return AI_JUDGE_SKIP
            prov_type = getattr(prov, "provider_type", None) or getattr(prov, "type", None)
            if prov_type and str(prov_type).lower() not in ("chat", "chat_completion", "llm"):
                logger.warning(f"[readair] ai_judge_provider is not a chat provider: {prov_type}")
                return AI_JUDGE_SKIP
        except Exception as e:
            logger.warning(f"[readair] ai_judge provider check failed: {e}")

        prompt = f"{history_block}\n\n{current_block}\n\n请仅输出 REPLY 或 SKIP。"
        try:
            resp = await self._context.llm_generate(
                chat_provider_id=judge_provider_id,
                prompt=prompt,
                system_prompt=system_prompt or "",
            )
        except Exception as e:
            logger.warning(f"[readair] ai_judge llm_generate failed: {e}")
            return AI_JUDGE_SKIP

        text = (getattr(resp, "completion_text", "") or "").strip().upper()
        # 朴素解析：包含 REPLY 即 REPLY，否则 SKIP
        if "REPLY" in text and "SKIP" not in text:
            return AI_JUDGE_REPLY
        if "REPLY" in text and "SKIP" in text:
            # 两者都有：取第一个出现的
            return AI_JUDGE_REPLY if text.find("REPLY") < text.find("SKIP") else AI_JUDGE_SKIP
        return AI_JUDGE_SKIP

    # ---------- 主回复（tool_loop_agent 优先，llm_generate 兜底） ----------
    async def _run_main_reply(
        self,
        *,
        event,
        umo: str,
        history_block: str,
        current_block: str,
    ) -> str:
        """沿用当前会话 chat provider。

        V3 策略：
        - **主路径**：``context.tool_loop_agent(event=..., chat_provider_id=..., ...)``
          走 Agent 路径以沿用当前会话 persona / tools / subagents 能力环境。
        - **工具集**：从 ``context.get_llm_tool_manager()`` 尽量还原当前会话已注册
          的函数工具（包括 subagent 编排注入的 HandoffTool），通过
          ``_build_session_toolset`` 构造 ToolSet 后传入。
          如果收集失败，**省略** ``tools`` 参数让 ``tool_loop_agent`` 使用其签名默认值
          （也是 None），而不是主动传 ``ToolSet([])`` 把能力清空。
        - **Fallback**：仅在 ``ToolSet`` 导入失败 / ``tool_loop_agent`` 不存在 /
          调用异常 / 返回空文本时，退到 ``context.llm_generate(...)``。

        两条路径都只产出一段**纯文本**回复；多段消息链 / 流式回复 属于 V1 扩展槽位。
        """
        try:
            prov_id = await self._context.get_current_chat_provider_id(umo)
        except Exception as e:
            logger.warning(f"[readair] get_current_chat_provider_id failed: {e}")
            return ""
        if not prov_id:
            logger.info("[readair] no current chat provider for umo; skip reply")
            return ""

        system_prompt_parts = []
        if history_block:
            system_prompt_parts.append(history_block)
        system_prompt_parts.append(
            "以上是历史对话。请只围绕【当前消息】作答，不要重复历史内容。"
        )
        system_prompt = "\n\n".join(system_prompt_parts)

        # ----- 主路径：tool_loop_agent -----
        used_agent = False
        if ToolSet is not None and hasattr(self._context, "tool_loop_agent"):
            used_agent = True
            # 收集当前会话已注册的 tools / subagents / handoff，避免传 ToolSet([]) 把能力清空
            session_tools = _build_session_toolset(self._context)
            try:
                # 当 session_tools 为 None 时不传 tools 参数，让 tool_loop_agent 使用其自身
                # 的默认值（也是 None），而不是显式传 ToolSet([]) 去覆盖。
                kwargs: dict = dict(
                    event=event,
                    chat_provider_id=prov_id,
                    prompt=current_block,
                    system_prompt=system_prompt,
                    max_steps=30,
                    tool_call_timeout=60,
                )
                if session_tools is not None:
                    kwargs["tools"] = session_tools
                resp = await self._context.tool_loop_agent(**kwargs)
                text = (getattr(resp, "completion_text", "") or "").strip()
                if text:
                    return text
                logger.info(
                    "[readair] tool_loop_agent returned empty text, "
                    "falling back to llm_generate"
                )
            except Exception as e:
                logger.warning(
                    f"[readair] tool_loop_agent failed, falling back to llm_generate: {e}"
                )
        else:
            logger.debug(
                "[readair] tool_loop_agent / ToolSet unavailable; using llm_generate directly"
            )

        # ----- Fallback：llm_generate -----
        try:
            resp = await self._context.llm_generate(
                chat_provider_id=prov_id,
                prompt=current_block,
                system_prompt=system_prompt,
            )
        except Exception as e:
            logger.error(
                f"[readair] main reply failed (agent_used={used_agent}): {e}",
                exc_info=True,
            )
            return ""
        return (getattr(resp, "completion_text", "") or "").strip()
