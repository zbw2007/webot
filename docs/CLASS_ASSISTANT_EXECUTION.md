# 班级事务助手执行记录

## 下载与基线

- 执行日期：2026-09-03（Asia/Shanghai）
- 工作目录：`C:\Users\27032\WeBot-ClassAssistant`
- 上游：`https://github.com/GuMu599/webot.git`
- 基线 commit：`19b82a2f941eeebcaed37e386c34c96bba69a23d`
- 当前分支：`feature/class-assistant`
- 当前 HEAD：`a22b67516585a78556fd5ca58ffd72398262d5f7`
- GitHub Fork：`https://github.com/zbw2007/webot.git`
- Fork 推送日期：2026-09-03（Asia/Shanghai）
- 初始 Git 状态：下载后工作树干净；CowAgent 目录未触碰。

## 环境

- Python：3.13.12，虚拟环境 `.venv`
- Node.js：24.15.0
- npm：11.12.1
- 微信：未登录、未读取消息，尚未确认版本兼容性
- `native/windows/wcdb_api.dll`：当前浅克隆中不存在，因此没有可记录的 DLL SHA-256；在 DLL 来源、版本和哈希确认前禁止接入主账号。

已通过可信镜像安装的运行依赖包括 Pillow、psutil、pyperclip、uiautomation、comtypes、pywebview、pywin32、Pydantic、OpenAI/Anthropic 客户端，以及 `silk-python`、`opencc-python-reimplemented`、`faster-whisper`；核心和可选语音模块均可导入，`pip check` 无破损依赖。

## 验证记录

```text
pytest tests/class_assistant -q  -> 98 passed
python -m compileall -q src tests -> 0
pip check -> No broken requirements found.
```

附加基线运行（排除当前 Windows 环境不适用的 `test_window_controller.py`）结果为 `359 passed, 42 failed, 11 skipped`；失败集中在上游既有 `MAX_RETRIES`/Feishu 断言和浅克隆缺失的 macOS 工具文件，班级助手专项测试全部通过。

上游全量测试仍有与本功能无关的既有失败：`MAX_RETRIES` 校验、Feishu secret 展示断言，以及浅克隆缺少 macOS 工具文件导致的测试失败。没有修改这些基线问题。

阶段一至五提交：`4f60227`（Fork/基线记录）、`376c30d`（前端锁定）、`89b929e` 至 `b4a806b`（WCDB preflight、只读群元数据发现、生命周期及日志脱敏加固）、`180ae3e`（Checkpoint 5 离线验收覆盖）以及 `a22b675`（本执行记录修订）。Fork 已绑定到 `https://github.com/zbw2007/webot.git`；前端使用 npm 官方源完成安装并生成 `ui/package-lock.json`；`npm audit --omit=dev` 报告 0 vulnerabilities，`npm run build` 已通过（仅有 Vite CommonJS 配置和 chunk 体积提示）。

## 安全状态

- `CLASS_ASSISTANT_ENABLED=false`、`REAL_SEND_ENABLED=false`、`DRY_RUN=true` 默认保持关闭/模拟。
- 启用助手时，后端轮询范围直接收敛到显式 `CLASS_ASSISTANT_GROUPS`；空白名单不轮询。
- 私聊、非白名单群不会写入助手数据库，也不会回退到旧版自动回复路由。
- 发送必须经过最新版本批准、一次性确认令牌、后端窗口/群名校验、原子 claim 和重复指纹校验。
- 发送异常保留 `sending`，只能人工通过 `mark-sent` / `mark-failed` 对账，不自动重发。
- API 只允许本地 Host/Origin；API Key 不返回给助手页面。

## 后续上线门槛

1. 补齐并审计官方/可信来源的 `native/windows/wcdb_api.dll`，确认来源、签名、批准哈希和微信版本兼容性。
2. 配置稳定的班级群 `chat_id`，保持真实发送关闭，完成“测试群只读 48 小时 → 正式群只读 48 小时 → 测试群模拟发送 48 小时”。
3. 只有人工审核和窗口验证均通过、且用户再次明确授权后，才由用户在本地控制台手动开启真实发送。

## Checkpoint 5：离线验收

- 专项测试覆盖白名单采集、私聊/非白名单拒绝、两个群独立分析、待办和草稿来源关联、编辑撤销旧批准、批准与一次性确认令牌、DRY_RUN 模拟发送（注入 sender 调用次数为 0）。
- 覆盖真实发送模式下的原子 claim 竞争：同一批准版本两个线程只有一个成功，sender 只调用一次。
- 覆盖发送器异常后的 `sending` 保留、显式失败对账为 `needs_reconciliation`，以及后续调用不会自动重发。
- DeepSeek/model 失败保持分析游标不变且不产生可发送草稿；非法 JSON 只重试一次。
- 本次离线验收：专项测试覆盖白名单采集、双群独立分析、来源关联、编辑撤销批准、一次性确认令牌（含复用拦截）、DRY_RUN 模拟发送、并发 claim、崩溃对账和模型失败回滚；`pytest tests/class_assistant -q` 实际结果为 `98 passed`。`python -m compileall -q src tests`、`pip check`、`git diff --check` 和前端 `npm run build` 均已通过。
- 当前保持 `CLASS_ASSISTANT_ENABLED=false`、`REAL_SEND_ENABLED=false`、`DRY_RUN=true`，未连接主微信、未读取真实群消息、未操作发送器。
- 48 小时外部测试仍未完成；可信 WCDB DLL 来源、签名、批准哈希和微信版本兼容性仍未确认，因此不能启动真实采集或进入真实账号上线阶段。
