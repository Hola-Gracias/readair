"""Layer 2 — 命中判定层。

判定顺序（硬约束，见设计文档四.4）：
    空消息 -> 会话准入 -> 用户黑名单 -> 屏蔽词 -> 触发词 -> 概率 -> AI 判定

本层只负责到 "概率"；AI 判定放在回复执行层前置 gate。

不负责：当前消息聚合、历史落盘、主回复生成、回复发送。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from .models import NormalizedInboundMessage


# 判定结论
ADMIT_NOT_TAKEN_OVER = "not_taken_over"   # 会话未准入，不接管、不存储
ADMIT_STORAGE_ONLY = "storage_only"        # 会话已准入但 enable_switch=off：仅写历史，不接管、不回复
ADMIT_TAKEN_OVER_DROP = "taken_over_drop"  # 已接管但不回复（黑名单/屏蔽词/概率失败）
ADMIT_CREATE_KEYWORD = "create_keyword"   # 命中 trigger，创建 keyword_triggered
ADMIT_CREATE_PROBABILITY = "create_probability"  # 未命中 trigger，概率通过，创建 probability_triggered


@dataclass
class GateDecision:
    verdict: str                           # 上述四个之一
    reason: str                            # 简短原因（用于调试日志）
    taken_over: bool                       # 是否已接管回复权（决定是否 stop_event）
    create_task: bool                      # 是否应创建候选回复任务


def _session_admitted(
    msg: NormalizedInboundMessage, cfg: dict[str, Any]
) -> tuple[bool, str]:
    """群聊 / 私聊 准入判定。"""
    if msg.chat_type == "private":
        mode = cfg.get("private_list_mode", "blacklist")
        lst = {str(x) for x in (cfg.get("private_list") or [])}
        target = msg.sender_id
    else:
        mode = cfg.get("group_list_mode", "blacklist")
        lst = {str(x) for x in (cfg.get("group_list") or [])}
        target = msg.group_id or msg.session_id

    if mode == "whitelist":
        return (str(target) in lst, f"{mode}:{target}")
    # blacklist
    return (str(target) not in lst, f"{mode}:{target}")


def _user_blacklisted(msg: NormalizedInboundMessage, cfg: dict[str, Any]) -> bool:
    lst = {str(x) for x in (cfg.get("group_user_blacklist") or [])}
    return msg.sender_id in lst


def _block_hit(text: str, cfg: dict[str, Any]) -> str | None:
    for kw in cfg.get("block_keywords") or []:
        kw = str(kw)
        if kw and kw in text:
            return kw
    return None


def hit_trigger(text: str, cfg: dict[str, Any]) -> str | None:
    """触发词命中判定（子串匹配）。对外暴露供防抖层做附着消息升级判定使用。"""
    for kw in cfg.get("trigger_keywords") or []:
        kw = str(kw)
        if kw and kw in text:
            return kw
    return None


def decide(
    msg: NormalizedInboundMessage, cfg: dict[str, Any], rng: random.Random | None = None
) -> GateDecision:
    """对一条已标准化的入站消息做命中判定。"""
    # 0. 空消息：上层已丢弃；防御性兜底
    if msg.is_empty:
        return GateDecision(ADMIT_NOT_TAKEN_OVER, "empty", False, False)

    # 1. 会话准入（必须先于 enable_switch）
    #    非准入 → 插件完全不碰这条消息（不接管、不存储、不 stop），交给本体处理。
    admitted, reason = _session_admitted(msg, cfg)
    if not admitted:
        return GateDecision(ADMIT_NOT_TAKEN_OVER, f"session_not_admitted:{reason}", False, False)

    # 2. 总开关只控制"回复主链路"，不控制存储（设计文档 四.2）
    #    enable_switch=off 时仍按存储策略写原始历史，但不接管、不 stop，本体可继续处理。
    if not cfg.get("enable_switch", True):
        return GateDecision(ADMIT_STORAGE_ONLY, "enable_switch=off", False, False)

    # 已进入插件主链路 -> 接管回复权
    # 后续无论是否回复，都不再放行本体默认 LLM。

    # 3. 用户黑名单
    if _user_blacklisted(msg, cfg):
        return GateDecision(ADMIT_TAKEN_OVER_DROP, "user_blacklisted", True, False)

    # 4. 屏蔽词
    hit = _block_hit(msg.match_text, cfg)
    if hit is not None:
        return GateDecision(ADMIT_TAKEN_OVER_DROP, f"block_keyword:{hit}", True, False)

    # 5. 触发词优先
    trig = hit_trigger(msg.match_text, cfg)
    if trig is not None:
        return GateDecision(ADMIT_CREATE_KEYWORD, f"trigger:{trig}", True, True)

    # 6. 概率采样
    prob = float(cfg.get("response_probability", 0.0) or 0.0)
    if prob <= 0.0:
        return GateDecision(ADMIT_TAKEN_OVER_DROP, "probability=0", True, False)
    roll = (rng or random).random()
    if roll < prob:
        return GateDecision(ADMIT_CREATE_PROBABILITY, f"probability:{roll:.3f}<{prob}", True, True)
    return GateDecision(ADMIT_TAKEN_OVER_DROP, f"probability:{roll:.3f}>={prob}", True, False)
