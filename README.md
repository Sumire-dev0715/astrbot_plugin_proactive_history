# 主动消息补记（astrbot_plugin_proactive_history）

解决一个具体的框架问题：**机器人主动发出的消息不会进对话记录。**

## 问题

未来任务（cron）/ 后台任务触发时，AstrBot 唤醒 agent 干活，agent 通过
`send_message_to_user` 工具把内容发给用户。这个工具只负责"发"，**不写会话历史**；
而任务收尾时 `persist_agent_history` 往历史里写的是一对合成消息：

```
user:      Output your last task result below.
assistant: [CronJob] 任务名: … triggered at …, I finished this job, here is the result: <模型自述>
```

结果：用户回看对话记录，看不到机器人到底说了什么（只有模型的转述）；模型自己也不知道
自己说过，会出现"欸？我真的戳到你了？那就是消息发出去了但没进我这边的记录"这类困惑。
（4.29.0 修复了 cron 的 misfire 宽限，但这个问题没有修。）

## 做法：不写数据库，把内容"喂"进请求

框架保存历史时存的是"**本轮实际发出去的消息列表**"。所以只要让补记内容成为这个列表的
一部分，框架自己就会把它持久化——全程只有一个写入方，不存在与框架"整份覆盖写"（基于
请求开始时的快照）抢写的竞态。

```
send_message_to_user 执行
   └─ on_llm_tool_respond 钩子：截获 messages 里的文本 → 存进补记队列（KV 持久化，按会话归档）

下一轮请求
   └─ on_llm_request 钩子：
      ① 清掉 cron/后台任务留下的合成脚手架（两条固定形态）
      ② 把队列里尚未落盘的内容作为 assistant 消息追加到上下文末尾
         （时间顺序正确：它确实发生在用户这条新消息之前）
      ③ 队列条目保留，直到在历史里确认落盘才出队（防止本轮保存失败丢内容）

本轮结束 → 框架保存"实际发送的消息列表" → 补记内容自然进记录 ✓
```

## 安装

把本目录整个放到 AstrBot 的 `data/plugins/` 下，重启 AstrBot（或 WebUI 里重载插件）。
无第三方依赖。

## 配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enable_capture` | 开 | 补记 `send_message_to_user` 发出的内容 |
| `enable_scaffold_cleanup` | 开 | 清理 `Output your last task result below.` / `[CronJob] …` / `[BackgroundTask] …` 合成消息 |
| `record_scope` | `conversation` | `conversation` = 只补进主动消息发出时所在的那个对话（严格，不会把旧消息带进新对话）；`session` = 不管在哪个对话回复都补进当前对话（更不容易丢） |
| `record_media_placeholder` | 开 | 图片/语音/视频/文件用占位符记录 |
| `max_pending_per_session` | 20 | 每会话待补记上限，超出丢弃最早的 |

## 验证

1. 让机器人设一个 2 分钟后的未来任务（升级到 4.29.0 后 5 分钟内的 misfire 都能触发）；
2. 收到主动消息后，随便回一句；
3. 看会话记录：主动消息的内容应该以 assistant 消息出现在你这条回复**之前**，
   且没有 `Output your last task result below.` 这类脚手架。
4. 插件日志（WebUI 日志页搜 `proactive_history`）会打印：
   `记下 1 条主动消息，等下一轮补入历史（…）` → `本轮补入 1 条主动消息（…）`。

## 边界与已知限制

- **没有下一轮就不补**：你一直不回消息时，补记留在队列里，等你下次说话时补上（位置仍正确）。
- **去重按内容指纹**（前 80 字符）：同一段文本连发两次时只会补一条。
- **不动框架的写入**：插件从不写数据库，所以框架自身"长命 run 用旧快照整份覆盖"的
  问题（一次性丢整段对话）不在本插件的职责范围。
- 只处理文本与媒体占位；工具发送的其他富消息类型按占位符记录。
