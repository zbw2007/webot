# 班级事务助手执行记录

## 下载与基线

- 执行日期：2026-09-03（Asia/Shanghai）
- 工作目录：`C:\Users\27032\WeBot-ClassAssistant`
- 上游：`https://github.com/GuMu599/webot.git`
- 基线 commit：`19b82a2f941eeebcaed37e386c34c96bba69a23d`
- 当前分支：`feature/class-assistant`
- 当前 HEAD：`e919172`
- GitHub Fork：未绑定。当前环境没有 GitHub CLI，且没有可用的已认证 Fork 远程；未伪造 `origin`，代码仍与上游隔离。
- 初始 Git 状态：下载后工作树干净；CowAgent 目录未触碰。

## 环境

- Python：3.13.12，虚拟环境 `.venv`
- Node.js：24.15.0
- npm：11.12.1
- 微信：未登录、未读取消息，尚未确认版本兼容性
- `native/windows/wcdb_api.dll`：当前浅克隆中不存在，因此没有可记录的 DLL SHA-256；在 DLL 来源、版本和哈希确认前禁止接入主账号。

已通过可信镜像安装的运行依赖包括 Pillow、psutil、pyperclip、uiautomation、comtypes、pywebview、pywin32、Pydantic、OpenAI/Anthropic 客户端等；`pip check` 无破损依赖。

## 验证记录

```text
pytest tests/class_assistant -q  -> 24 passed
python -m compileall -q src tests -> 0
pip check -> No broken requirements found.
```

上游全量测试仍有与本功能无关的既有失败：`MAX_RETRIES` 校验、Feishu secret 展示断言，以及浅克隆缺少 macOS 工具文件导致的测试失败。没有修改这些基线问题。

前端 `npm install` 已使用 npmjs 与 npmmirror/清华镜像多次尝试，均因网络长时间无响应而中止；当前没有 `ui/node_modules`，所以尚未生成 `ui/dist`，也没有宣称前端构建通过。

## 安全状态

- `CLASS_ASSISTANT_ENABLED=false`、`REAL_SEND_ENABLED=false`、`DRY_RUN=true` 默认保持关闭/模拟。
- 启用助手时，后端轮询范围直接收敛到显式 `CLASS_ASSISTANT_GROUPS`；空白名单不轮询。
- 私聊、非白名单群不会写入助手数据库，也不会回退到旧版自动回复路由。
- 发送必须经过最新版本批准、一次性确认令牌、后端窗口/群名校验、原子 claim 和重复指纹校验。
- 发送异常保留 `sending`，只能人工通过 `mark-sent` / `mark-failed` 对账，不自动重发。
- API 只允许本地 Host/Origin；API Key 不返回给助手页面。

## 后续上线门槛

1. 补齐并审计官方/可信来源的 `native/windows/wcdb_api.dll`，记录哈希和微信版本兼容性。
2. 绑定用户自己的 GitHub Fork（如需要提交上游）。
3. 解决前端依赖网络问题并运行 `npm run build`。
4. 配置稳定的班级群 `chat_id`，保持真实发送关闭，完成“测试群只读 48 小时 → 正式群只读 48 小时 → 测试群模拟发送 48 小时”。
5. 只有人工审核和窗口验证均通过后，才由用户在本地控制台手动开启真实发送。
