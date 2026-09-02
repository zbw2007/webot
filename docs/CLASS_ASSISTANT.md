# WeBot 班级事务助手

班级事务助手是 WeBot 的可选、白名单优先功能：只保存明确配置的群聊消息，在北京时间 08:00 和 20:00 分群分析，生成待办和回复草稿。草稿必须经过编辑/批准后才能进入发送流程。

## 安全默认值

```dotenv
CLASS_ASSISTANT_ENABLED=false
CLASS_ASSISTANT_GROUPS=
COLLECTION_ENABLED=false
ANALYSIS_ENABLED=false
REVIEW_QUEUE_ENABLED=true
REAL_SEND_ENABLED=false
DRY_RUN=true
TIMEZONE=Asia/Shanghai
DIGEST_SCHEDULE=08:00,20:00
```

`CLASS_ASSISTANT_GROUPS` 只能填写稳定的 `chat_id`，不能填写 `*`。私聊和不在白名单内的群聊不会进入助手数据库；助手回调始终返回空值，因此不会借用 WeChat 后端的自动回复路径。

## 启用只读模式

1. 先启动 WeBot，确认微信数据库读取与 AI 配置均正常。
2. 从日志或诊断页取得班级群的稳定 `chat_id`，写入 `CLASS_ASSISTANT_GROUPS`（多个 ID 用逗号分隔）。
3. 依次将 `CLASS_ASSISTANT_ENABLED`、`COLLECTION_ENABLED`、`ANALYSIS_ENABLED` 设为 `true`，保持 `REAL_SEND_ENABLED=false`、`DRY_RUN=true`。
4. 打开控制台的“班级事务助手”页，确认白名单、汇总运行记录、待办和草稿。

模型输出必须是 JSON，并经过 Pydantic（可用时）与字段级校验；校验失败时本次汇总标记为失败，分析游标不前进。

## 审核和发送

审核状态为 `pending_review → edited → approved → sending → sent`。编辑会创建新版本并撤销旧批准；高风险内容（请假、成绩、费用、承诺、投诉、隐私）必须先编辑。发送接口需要最新版本和一次性确认令牌。发送前还会验证白名单、群名和重复指纹。真实发送前由 WeChat 后端自行解析 `chat_id`、检查当前可见窗口标题和登录状态；浏览器提交的窗口名不被当作安全依据。

在 `DRY_RUN=true` 时会完整执行校验，但不会调用 WeChat 发送器。发送器崩溃会留下 `sending` 状态，需人工核对，不会自动重发。

## 本地 API

所有接口只由本地 Web 服务提供：

* `GET /api/class-assistant/status`
* `GET /api/class-assistant/digests`
* `GET /api/class-assistant/todos`
* `GET /api/class-assistant/drafts`
* `GET /api/class-assistant/groups`
* `GET /api/class-assistant/audit`
* `POST /api/class-assistant/drafts/{id}/approve|reject|edit|send`
* `POST /api/class-assistant/drafts/{id}/mark-sent|mark-failed`（发送崩溃后的人工对账）
* `POST /api/class-assistant/token`
* `POST /api/class-assistant/stop`

API 不返回任何模型 API Key。`/stop` 是进程内紧急停止，会停止定时任务并阻止后续采集；重新启动 WeBot 才能恢复。

## 数据留存

默认原始消息保留 7 天，回复草稿 30 天，审计记录 30 天，待办长期保留。清理任务只删除到期记录，日志不写入完整聊天内容。

## 验证

```powershell
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe -m pytest tests\class_assistant -q
.\.venv\Scripts\python.exe -m compileall -q src tests
```

真实发送上线前必须完成：测试群只读 48 小时、正式群只读 48 小时、测试群模拟发送 48 小时，然后才逐步人工批准真实发送。默认永久关闭真实发送，不应在未审计窗口控制器和账号风险前打开。
