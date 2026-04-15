"""Layer 4 — 上下文 / 存储层：主历史账本。

硬约束（设计文档 十四）：
- 同一 UMO 的 "读取快照 -> 追加原始记录 -> 更新窗口 / 任务状态" 必须在同一串行临界区内完成。
- 不同 UMO 之间允许并行。

实现：每个 (platform, chat_type, id) 维护一个 asyncio.Lock + 内存缓存；
写入时 JSON 落盘。卸载时由上层调用 flush。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from .models import NormalizedInboundMessage

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 仅在非 AstrBot 环境下兜底
    import logging
    logger = logging.getLogger("readair.storage")


HISTORY_SCHEMA_VERSION = 2


class HistoryStore:
    """主历史账本。

    存储路径：
        data/plugin_data/{plugin_name}/history/{platform_id}/group/{group_id}.json
        data/plugin_data/{plugin_name}/history/{platform_id}/private/{user_id}.json
    """

    def __init__(self, base_dir: Path):
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)
        # 每个账本一把锁；key 为 文件路径。
        self._locks: dict[str, asyncio.Lock] = {}
        # 内存缓存：path -> 账本 dict
        self._cache: dict[str, dict[str, Any]] = {}

    # --- 内部工具 ---
    def _path_for(self, platform_id: str, chat_type: str, target_id: str) -> Path:
        safe_pid = platform_id.replace("/", "_") or "unknown"
        safe_id = (target_id or "unknown").replace("/", "_")
        sub = "group" if chat_type == "group" else "private"
        return self._base_dir / safe_pid / sub / f"{safe_id}.json"

    def _lock_for(self, path: Path) -> asyncio.Lock:
        key = str(path)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _load_sync(self, path: Path, platform_id: str, chat_type: str, session_id: str) -> dict[str, Any]:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[readair] history file corrupted, recreating: {path} ({e})")
        return {
            "version": HISTORY_SCHEMA_VERSION,
            "platform": platform_id,
            "chat_type": chat_type,
            "session_id": session_id,
            "records": [],
        }

    def _save_sync(self, path: Path, ledger: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _get_ledger(self, platform_id: str, chat_type: str, target_id: str, session_id: str) -> tuple[Path, dict[str, Any]]:
        path = self._path_for(platform_id, chat_type, target_id)
        key = str(path)
        ledger = self._cache.get(key)
        if ledger is None:
            ledger = self._load_sync(path, platform_id, chat_type, session_id)
            self._cache[key] = ledger
        return path, ledger

    def _trim(self, ledger: dict[str, Any], max_count: int) -> None:
        records = ledger.get("records", [])
        if max_count > 0 and len(records) > max_count:
            # 从头部移除最旧记录
            del records[: len(records) - max_count]

    # --- 公共 API ---
    def _target_id_for(self, msg: NormalizedInboundMessage) -> str:
        if msg.chat_type == "group":
            return msg.group_id or msg.session_id
        return msg.sender_id or msg.session_id

    async def snapshot_and_append_user(
        self,
        msg: NormalizedInboundMessage,
        *,
        context_message_limit: int,
        storage_max_count: int,
        enable_private_chat_storage: bool,
        write: bool,
    ) -> list[dict[str, Any]]:
        """关键的同步临界区：
        1) 读取快照（不含当前触发消息本身）
        2) 追加当前原始消息到主账本
        3) 返回快照

        write=False 时只读快照不写入（例如命中屏蔽词 + 私聊存储关闭的场景）。
        """
        if msg.chat_type == "private" and not enable_private_chat_storage:
            write = False

        target = self._target_id_for(msg)
        path = self._path_for(msg.platform_id, msg.chat_type, target)
        async with self._lock_for(path):
            _, ledger = self._get_ledger(msg.platform_id, msg.chat_type, target, msg.session_id)

            # 1. 快照 —— 取最近 context_message_limit 条，不含当前
            records = ledger.setdefault("records", [])
            if context_message_limit > 0:
                snapshot = list(records[-context_message_limit:])
            else:
                snapshot = list(records)

            # 2. 追加
            if write:
                records.append({
                    "type": "user_message",
                    "ts": msg.ingest_ts,
                    "platform_ts": msg.platform_ts,
                    "ingest_seq": msg.ingest_seq,
                    "message_id": msg.message_id,
                    "sender_id": msg.sender_id,
                    "sender_name": msg.sender_name,
                    "text": msg.display_text,
                    "raw_text": msg.raw_text,
                    "component_types": msg.raw_component_types,
                })
                self._trim(ledger, storage_max_count)
                self._save_sync(path, ledger)

            return snapshot

    async def append_user_only(
        self,
        msg: NormalizedInboundMessage,
        *,
        storage_max_count: int,
        enable_private_chat_storage: bool,
    ) -> None:
        """仅追加原始消息（附着消息、屏蔽词消息等都走这里）。"""
        if msg.chat_type == "private" and not enable_private_chat_storage:
            return
        target = self._target_id_for(msg)
        path = self._path_for(msg.platform_id, msg.chat_type, target)
        async with self._lock_for(path):
            _, ledger = self._get_ledger(msg.platform_id, msg.chat_type, target, msg.session_id)
            records = ledger.setdefault("records", [])
            records.append({
                "type": "user_message",
                "ts": msg.ingest_ts,
                "platform_ts": msg.platform_ts,
                "ingest_seq": msg.ingest_seq,
                "message_id": msg.message_id,
                "sender_id": msg.sender_id,
                "sender_name": msg.sender_name,
                "text": msg.display_text,
                "raw_text": msg.raw_text,
                "component_types": msg.raw_component_types,
            })
            self._trim(ledger, storage_max_count)
            self._save_sync(path, ledger)

    async def append_assistant(
        self,
        *,
        platform_id: str,
        chat_type: str,
        target_id: str,
        session_id: str,
        text: str,
        storage_max_count: int,
        task_id: str | None = None,
    ) -> None:
        """发送成功后写入 assistant_message。"""
        path = self._path_for(platform_id, chat_type, target_id)
        async with self._lock_for(path):
            _, ledger = self._get_ledger(platform_id, chat_type, target_id, session_id)
            records = ledger.setdefault("records", [])
            records.append({
                "type": "assistant_message",
                "ts": time.time(),
                "text": text,
                "task_id": task_id,
            })
            self._trim(ledger, storage_max_count)
            self._save_sync(path, ledger)

    async def append_generic(
        self,
        *,
        platform_id: str,
        chat_type: str,
        target_id: str,
        session_id: str,
        record: dict[str, Any],
        storage_max_count: int,
    ) -> None:
        """写入任意一种扩展记录类型（tool_call / tool_result / proactive_* 等）。

        调用方自己负责根据配置开关决定是否调用。
        """
        path = self._path_for(platform_id, chat_type, target_id)
        async with self._lock_for(path):
            _, ledger = self._get_ledger(platform_id, chat_type, target_id, session_id)
            records = ledger.setdefault("records", [])
            records.append(record)
            self._trim(ledger, storage_max_count)
            self._save_sync(path, ledger)

    async def flush_all(self) -> None:
        """卸载时调用：把所有内存缓存刷盘。"""
        for key, ledger in list(self._cache.items()):
            path = Path(key)
            try:
                async with self._lock_for(path):
                    self._save_sync(path, ledger)
            except Exception as e:
                logger.warning(f"[readair] flush failed for {path}: {e}")
