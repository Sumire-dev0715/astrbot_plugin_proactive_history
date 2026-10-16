# 更新日志

## v1.1.0

- **修复：补记写进了记录、模型却看不到。** 框架的顺序是
  `OnWaitingLLMRequestEvent`（拿锁前）→ 获取会话锁 → 构建 agent（读取并快照会话历史）→
  `OnLLMRequestEvent`。原先在 `OnLLMRequestEvent` 里追加 `req.contexts`，
  此时 agent 已按旧历史构建完毕：补记只会随着本轮保存落进记录，**本轮请求里没有它**。
  改为在 `on_waiting_llm_request` 里直接把内容写进会话历史，构建、请求、保存都会带上。
- 补记写入的消息形态与框架一致（`content` 用分片列表，不再用裸字符串）。
- 新增 `record_scope`：`conversation`（默认，只补进主动消息发出时所在的那个对话）/ `session`。
- 只改队列（条目出队）时不再多余地回写会话历史。

## v1.0.0

- 捕获 `send_message_to_user` 发出的内容，在下一轮补入对话记录。
- 清理 cron/后台任务写进历史的合成脚手架消息。
