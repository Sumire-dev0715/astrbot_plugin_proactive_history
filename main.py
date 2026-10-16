"""把机器人主动发出的消息补记进对话记录。

AstrBot 的未来任务（cron）/后台任务在收尾时，只往会话历史里写两条合成消息
（`Output your last task result below.` + `[CronJob] …`），而真正通过
`send_message_to_user` 发给用户的内容不进历史：用户回看记录看不到机器人说过什么，
模型自己也不知道自己说过。

本插件做两件事：

1. 捕获：`on_llm_tool_respond` 里截获 `send_message_to_user` 发出的文本，存进补记队列
   （KV 持久化，按会话归档，记下发出时所在的对话）。
2. 补写：`on_waiting_llm_request` 里把队列中尚未落盘的内容写进会话历史，并摘掉合成脚手架。

为什么补写必须放在 `on_waiting_llm_request`：框架的执行顺序是
`OnWaitingLLMRequestEvent`（拿锁前）→ 获取会话锁 → 构建 agent（此时读取会话历史并快照成
本轮消息列表）→ `OnLLMRequestEvent`（插件改 req.contexts）。等 `OnLLMRequestEvent` 再追加，
agent 早已按旧历史构建完毕：改动只会随着本轮保存落进记录，**模型这一轮看不到**——
表现为"记录里有、模型坚称没有"。所以在等待钩子里直接把内容写进会话历史，
后续的构建、请求、保存都会带上它。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_proactive_history"
JOURNAL_KEY = "pending_sends"
SEND_TOOL = "send_message_to_user"

# cron / 后台任务收尾时写进历史的固定形态（history_saver.persist_agent_history）
SCAFFOLD_USER_TEXTS = {"output your last task result below."}
SCAFFOLD_ASSISTANT_PREFIXES = ("[cronjob]", "[backgroundtask]")

MEDIA_PLACEHOLDER = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "mention_user": "[@用户]",
}

# 判定"这条内容已在历史里"时只比对前若干字符：注入的消息可能被平台的上下文处理裁剪
FINGERPRINT_CHARS = 80


def _fingerprint(text: str) -> str:
    return " ".join((text or "").split())[:FINGERPRINT_CHARS]


def _role_of(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or "")
    return str(getattr(message, "role", "") or "")


def _text_of(message: Any) -> str:
    content = (
        message.get("content")
        if isinstance(message, dict)
        else getattr(message, "content", None)
    )
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in (None, "text"):
                    parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(getattr(part, "text", "") or ""))
        return "\n".join(p for p in parts if p)
    return ""


def _is_scaffold(message: Any) -> bool:
    role = _role_of(message)
    text = _text_of(message).strip()
    if not text:
        return False
    if role == "user":
        return text.lower() in SCAFFOLD_USER_TEXTS
    if role == "assistant":
        lowered = text.lower()
        return any(lowered.startswith(prefix) for prefix in SCAFFOLD_ASSISTANT_PREFIXES)
    return False


def _send_failed(tool_result: Any) -> bool:
    """发送工具报错时不补记（结果以 error 开头，或 MCP 结果标了 isError）。"""
    if tool_result is None:
        return False
    if bool(getattr(tool_result, "isError", False)):
        return True
    content = getattr(tool_result, "content", None)
    texts: list[str] = []
    if isinstance(content, list):
        texts = [str(getattr(part, "text", "") or "") for part in content]
    elif isinstance(content, str):
        texts = [content]
    return " ".join(texts).strip().lower().startswith("error")


def _extract_texts(tool_args: dict, media_placeholder: bool) -> list[str]:
    messages = tool_args.get("messages")
    if not isinstance(messages, list):
        return []
    texts: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower()
        if kind == "plain":
            text = str(item.get("text") or "").strip()
            if text:
                texts.append(text)
        elif media_placeholder:
            placeholder = MEDIA_PLACEHOLDER.get(kind)
            if placeholder:
                texts.append(placeholder)
    return texts


def _assistant_message(text: str) -> dict:
    """按框架自身的消息形态写入：content 用分片列表，不用裸字符串。"""
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


@register(
    PLUGIN_NAME,
    "zcode",
    "补记主动消息进对话记录，并清理 cron/后台任务的合成脚手架消息",
    "v1.1.0",
)
class ProactiveHistoryPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # umo -> [{"id", "ts", "cid", "text"}]
        self._journal: dict[str, list[dict]] = {}

    async def initialize(self) -> None:
        try:
            stored = await self.get_kv_data(JOURNAL_KEY, {})
            if isinstance(stored, dict):
                self._journal = {
                    str(umo): [e for e in entries if isinstance(e, dict) and e.get("text")]
                    for umo, entries in stored.items()
                    if isinstance(entries, list)
                }
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 补记队列读取失败，按空队列继续")
        pending = sum(len(v) for v in self._journal.values())
        if pending:
            logger.info(f"[{PLUGIN_NAME}] 恢复了 {pending} 条未落盘的补记")

    async def terminate(self) -> None:
        await self._flush()

    # ---------------------------------------------------------------- 捕获

    @filter.on_llm_tool_respond()
    async def capture_send(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None,
        tool_result: Any,
    ) -> None:
        try:
            if not self._cfg_bool("enable_capture", True):
                return
            if str(getattr(tool, "name", "") or "") != SEND_TOOL:
                return

            args = tool_args if isinstance(tool_args, dict) else {}
            if _send_failed(tool_result):
                logger.debug(f"[{PLUGIN_NAME}] 发送失败，不补记")
                return
            texts = _extract_texts(args, self._cfg_bool("record_media_placeholder", True))
            if not texts:
                return

            target = args.get("session")
            umo = str(target or event.unified_msg_origin)
            entries = self._journal.setdefault(umo, [])
            entries.append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "ts": int(time.time()),
                    "cid": await self._current_cid(umo),
                    "text": "\n".join(texts),
                }
            )
            limit = max(1, self._cfg_int("max_pending_per_session", 20))
            if len(entries) > limit:
                dropped = len(entries) - limit
                del entries[:-limit]
                logger.warning(f"[{PLUGIN_NAME}] {umo} 待补记超过 {limit} 条，丢弃最早的 {dropped} 条")
            await self._flush()
            logger.info(f"[{PLUGIN_NAME}] 记下 1 条主动消息，等下一轮补入历史（{umo}）")
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 捕获主动消息失败")

    # ---------------------------------------------------------------- 补写

    @filter.on_waiting_llm_request()
    async def reconcile_history(self, event: AstrMessageEvent) -> None:
        """在 agent 构建（读取会话历史）之前动历史：摘脚手架、补主动消息。"""
        try:
            umo = str(event.unified_msg_origin)
            entries = self._journal.get(umo) or []
            cleanup_on = self._cfg_bool("enable_scaffold_cleanup", True)
            if not entries and not cleanup_on:
                return

            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return
            cid = str(cid)
            conversation = await manager.get_conversation(umo, cid)
            if conversation is None:
                return
            raw = getattr(conversation, "history", "") or "[]"
            history = json.loads(raw) if isinstance(raw, str) else list(raw)
            if not isinstance(history, list):
                return

            changed = False
            journal_changed = False

            # ① 摘掉 cron / 后台任务留下的合成脚手架
            if cleanup_on:
                kept = [m for m in history if not _is_scaffold(m)]
                removed = len(history) - len(kept)
                if removed:
                    history = kept
                    changed = True
                    logger.info(f"[{PLUGIN_NAME}] 清理了 {removed} 条合成脚手架（{umo}）")

            # ② 补记主动消息：确认已落盘的出队，未落盘的追加
            if entries:
                present = {_fingerprint(_text_of(m)) for m in history}
                kept_entries = [
                    e
                    for e in entries
                    if _fingerprint(str(e.get("text") or "")) not in present
                ]
                if len(kept_entries) != len(entries):
                    self._journal[umo] = kept_entries
                    journal_changed = True
                pending = [e for e in kept_entries if self._in_scope(e, cid)]
                if pending:
                    history.extend(_assistant_message(str(e["text"])) for e in pending)
                    changed = True
                    logger.info(
                        f"[{PLUGIN_NAME}] 补入 {len(pending)} 条主动消息（{umo} / 对话 {cid[:8]}）"
                    )

            if changed:
                await manager.update_conversation(umo, cid, history=history)
            if changed or journal_changed:
                await self._flush()
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 补写会话历史失败")

    # ---------------------------------------------------------------- 工具

    async def _flush(self) -> None:
        try:
            await self.put_kv_data(JOURNAL_KEY, self._journal)
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 补记队列写入失败")

    async def _current_cid(self, umo: str) -> str | None:
        """记下这次主动消息发出时该会话正处在哪个对话。"""
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            return str(cid) if cid else None
        except Exception:
            logger.debug(f"[{PLUGIN_NAME}] 取当前对话 id 失败，按会话级处理")
            return None

    def _in_scope(self, entry: dict, current_cid: str | None) -> bool:
        """record_scope=conversation（默认）时，只补进它发出时所在的那个对话；
        session 时不做限制。任一方拿不到对话 id 就退回会话级，避免内容丢失。"""
        if str(self._cfg("record_scope", "conversation")).strip().lower() == "session":
            return True
        entry_cid = entry.get("cid")
        if not entry_cid or not current_cid:
            return True
        return str(entry_cid) == str(current_cid)

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return bool(self._cfg(key, default))

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default
