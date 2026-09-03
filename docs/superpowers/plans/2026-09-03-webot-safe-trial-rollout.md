# WeBot Safe Trial and Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前 `feature/class-assistant` 分支补齐为可复现构建、可验证 WCDB 来源、可发现稳定群 ID，并按只读、模拟发送、测试群真实发送的顺序安全试运行。

**Architecture:** 保留现有白名单采集、半日分析、审核队列和安全发送架构，在 Bot 启动之前增加独立的运行前检查层。群发现只读取会话元数据，不读取消息正文；所有真实发送仍经过版本批准、一次性确认令牌、后端窗口校验和数据库原子 claim。

**Tech Stack:** Python 3.13、pytest、SQLite、Pydantic v2、WCDB ctypes、React 19、Vite 8、PowerShell、Git。

---

## 文件结构与职责

- Create: `src/class_assistant/preflight.py` — 检查平台、Python、DLL、哈希清单、配置和安全开关。
- Create: `src/class_assistant/group_discovery.py` — 只返回群 `chat_id`、显示名称和人数，不读取聊天正文。
- Create: `tests/class_assistant/test_preflight.py` — 运行前检查的失败优先测试。
- Create: `tests/class_assistant/test_group_discovery.py` — 群发现不读取消息的契约测试。
- Create: `scripts/verify_wcdb_runtime.ps1` — 生成 DLL SHA-256、签名和本机版本报告。
- Create: `docs/WCDB_PROVENANCE.md` — 固化 DLL 来源、哈希、扫描和兼容性结论。
- Modify: `src/bot.py` — 在创建 WCDB 后端和采集线程前执行 preflight。
- Modify: `src/wechat/wcdb_backend.py` — 以线程安全的公开方法提供群元数据发现，不暴露底层客户端。
- Modify: `src/class_assistant/service.py` — 注入并调用群发现函数，Web 层不直接访问 WCDB 客户端。
- Modify: `src/web/server.py` — 暴露只读 preflight/group-discovery API。
- Modify: `ui/src/components/ClassAssistantPanel.jsx` — 显示运行前检查、发现群并只保存稳定 `chat_id`。
- Modify: `docs/CLASS_ASSISTANT.md` — 增加构建、试运行和停止条件。
- Modify: `docs/CLASS_ASSISTANT_EXECUTION.md` — 记录每个检查点的实际证据。

## Checkpoint 1：冻结当前基线并绑定个人 Fork

**Files:**

- Modify: `.git/config`（只通过 `git remote` 命令修改）
- Modify: `docs/CLASS_ASSISTANT_EXECUTION.md`

- [ ] **Step 1: 提交用户已经批准的本计划**

Run:

```powershell
Set-Location C:\Users\27032\WeBot-ClassAssistant
git add --sparse docs/superpowers/plans/2026-09-03-webot-safe-trial-rollout.md
git commit -m "docs: plan safe WeBot trial rollout"
```

Expected: 只提交本计划文件，不夹带其他工作树改动。

- [ ] **Step 2: 验证工作树和当前提交**

Run:

```powershell
Set-Location C:\Users\27032\WeBot-ClassAssistant
git status --short
git rev-parse HEAD
git branch --show-current
git remote -v
```

Expected: 工作树无输出；分支为 `feature/class-assistant`；记录本计划提交后的 HEAD；只有 `upstream` 时进入下一步。

- [ ] **Step 3: 在 GitHub 网页创建 `GuMu599/WeBot` 的个人 Fork**

打开 `https://github.com/GuMu599/webot/fork`，保持仓库名 `webot`。这是外部写操作，必须由用户在已登录的 GitHub 页面确认。

- [ ] **Step 4: 读取用户确认后的 Fork URL 并绑定 origin**

Run:

```powershell
$forkUrl = Read-Host '粘贴 GitHub Fork 的 HTTPS clone URL'
if ($forkUrl -notmatch '^https://github\.com/[^/]+/webot(?:\.git)?$') { throw 'Fork URL 格式不符合预期' }
git remote add origin $forkUrl
git remote set-url --push upstream DISABLED
git remote -v
```

Expected: `origin` 指向用户 Fork；`upstream` fetch 仍指向 `GuMu599/webot.git`，push 显示 `DISABLED`。

- [ ] **Step 5: 推送功能分支**

Run:

```powershell
git push -u origin feature/class-assistant
```

Expected: GitHub Fork 出现 `feature/class-assistant`，本地分支跟踪 `origin/feature/class-assistant`。

- [ ] **Step 6: 更新执行记录并提交**

在 `docs/CLASS_ASSISTANT_EXECUTION.md` 记录 Fork URL、推送日期和 HEAD；不得写入访问令牌。

Run:

```powershell
git add --sparse docs/CLASS_ASSISTANT_EXECUTION.md
git commit -m "docs: record fork and rollout baseline"
git push
```

## Checkpoint 2：补齐前端可复现构建

**Files:**

- Create: `ui/package-lock.json`
- Test: `ui/src/components/ClassAssistantPanel.jsx`

- [ ] **Step 1: 清点 npm 环境，不删除任何已有目录**

Run:

```powershell
Set-Location C:\Users\27032\WeBot-ClassAssistant\ui
node --version
npm --version
Test-Path node_modules
Test-Path package-lock.json
```

Expected: Node `v24.15.0`、npm `11.12.1`；首次执行允许两个路径均为 `False`。

- [ ] **Step 2: 依次尝试三个可信 registry，每次最长等待 180 秒**

Run each command separately; the first success ends this step:

```powershell
npm install --registry=https://registry.npmjs.org --fetch-timeout=30000 --fetch-retries=2
npm install --registry=https://registry.npmmirror.com --fetch-timeout=30000 --fetch-retries=2
npm install --registry=https://mirrors.cloud.tencent.com/npm/ --fetch-timeout=30000 --fetch-retries=2
```

Expected: 生成 `node_modules` 和 `package-lock.json`。若三个源均失败，停止本检查点，保留网络错误，不从未知网盘下载依赖。

- [ ] **Step 3: 审计锁文件来源**

Run:

```powershell
Select-String -Path package-lock.json -Pattern 'resolved' | Select-Object -First 20
npm audit --omit=dev
```

Expected: `resolved` 只出现所选可信 registry；生产依赖没有 critical 漏洞。出现 critical 时停止，不构建发行版本。

- [ ] **Step 4: 构建并检查产物**

Run:

```powershell
npm run build
Get-ChildItem dist -Recurse -File | Select-Object FullName,Length
```

Expected: Vite 退出码为 0；`dist/index.html` 和静态资源存在。

- [ ] **Step 5: 提交锁文件**

Run:

```powershell
Set-Location C:\Users\27032\WeBot-ClassAssistant
git add ui/package-lock.json
git commit -m "build: lock frontend dependencies"
git push
```

## Checkpoint 3：建立 WCDB 来源清单和运行前硬门槛

**Files:**

- Create: `scripts/verify_wcdb_runtime.ps1`
- Create: `docs/WCDB_PROVENANCE.md`
- Create: `src/class_assistant/preflight.py`
- Create: `tests/class_assistant/test_preflight.py`
- Modify: `src/bot.py`

- [ ] **Step 1: 写失败测试——缺 DLL 时禁止启动**

Add to `tests/class_assistant/test_preflight.py`:

```python
from pathlib import Path

from src.class_assistant.preflight import PreflightReport, run_preflight


class Config:
    class_assistant_enabled = True
    class_assistant_groups = ["10001@chatroom"]
    class_assistant_real_send_enabled = False
    class_assistant_dry_run = True


def test_missing_wcdb_dll_blocks_start(tmp_path: Path):
    report = run_preflight(Config(), project_root=tmp_path)
    assert isinstance(report, PreflightReport)
    assert report.ok is False
    assert "wcdb_api.dll" in " ".join(report.errors)
```

- [ ] **Step 2: 写失败测试——真实发送和 DRY_RUN 同时开启时禁止启动**

Add:

```python
def test_real_send_requires_dry_run_to_be_disabled_only_after_rollout(tmp_path: Path):
    dll = tmp_path / "native" / "windows" / "wcdb_api.dll"
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b"test-dll")
    config = Config()
    config.class_assistant_real_send_enabled = True
    config.class_assistant_dry_run = True
    report = run_preflight(config, project_root=tmp_path, allowed_hashes={"ignored"})
    assert report.ok is False
    assert any("REAL_SEND_ENABLED" in error for error in report.errors)
```

- [ ] **Step 3: 运行测试确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\class_assistant\test_preflight.py -q
```

Expected: FAIL because `src.class_assistant.preflight` does not exist.

- [ ] **Step 4: 实现最小 preflight**

Create `src/class_assistant/preflight.py`:

```python
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    dll_path: str = ""
    dll_sha256: str = ""
    errors: tuple[str, ...] = field(default_factory=tuple)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_preflight(config: Any, project_root: Path, allowed_hashes: Iterable[str] = ()) -> PreflightReport:
    errors: list[str] = []
    groups = tuple(getattr(config, "class_assistant_groups", ()) or ())
    if not groups or "*" in groups:
        errors.append("CLASS_ASSISTANT_GROUPS must contain explicit stable chat_id values")
    if bool(getattr(config, "class_assistant_real_send_enabled", False)) and bool(
        getattr(config, "class_assistant_dry_run", True)
    ):
        errors.append("REAL_SEND_ENABLED requires an explicit post-rollout configuration with DRY_RUN=false")
    dll = project_root / "native" / "windows" / "wcdb_api.dll"
    if not dll.is_file():
        errors.append("native/windows/wcdb_api.dll is missing")
        return PreflightReport(False, str(dll), "", tuple(errors))
    digest = _sha256(dll)
    allowed = {value.lower() for value in allowed_hashes}
    if not allowed:
        errors.append("WCDB_ALLOWED_SHA256 must contain at least one reviewed hash")
    elif digest.lower() not in allowed:
        errors.append("wcdb_api.dll SHA-256 is not in the reviewed allowlist")
    return PreflightReport(not errors, str(dll), digest, tuple(errors))
```

- [ ] **Step 5: 运行测试确认通过**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\class_assistant\test_preflight.py -q
```

Expected: 2 passed。

- [ ] **Step 6: 创建 DLL 检查脚本**

Create `scripts/verify_wcdb_runtime.ps1`:

```powershell
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$dllPath = Join-Path $projectRoot 'native\windows\wcdb_api.dll'
if (-not (Test-Path -LiteralPath $dllPath -PathType Leaf)) { throw "Missing: $dllPath" }
$dll = Get-Item -LiteralPath $dllPath
$hash = Get-FileHash -LiteralPath $dllPath -Algorithm SHA256
$signature = Get-AuthenticodeSignature -LiteralPath $dllPath
[pscustomobject]@{
    Path = $dll.FullName
    Length = $dll.Length
    SHA256 = $hash.Hash
    SignatureStatus = $signature.Status
    Signer = $signature.SignerCertificate.Subject
} | Format-List
```

- [ ] **Step 7: 取得 DLL 但不加载**

只允许两种来源：上游项目的已签名 Release 附件，或从可审计源码在本机复现构建。下载后先放到隔离目录，不要直接放进 `native/windows`，先运行 Windows Defender 扫描并记录来源 URL、Release tag、文件大小、SHA-256、签名状态。

Stop condition: 来源页面、构建源码或哈希无法确认时，停止；不得接入主微信。

- [ ] **Step 8: 写入来源文档并建立哈希允许列表**

`docs/WCDB_PROVENANCE.md` 必须包含：来源 URL、下载日期、Release tag/commit、SHA-256、签名状态、Defender 结果、验证人、兼容的微信版本。随后把批准哈希通过本地 `.env` 的 `WCDB_ALLOWED_SHA256` 配置，不把凭证写入 Git。

- [ ] **Step 9: Bot 启动前接入 preflight**

Modify `src/bot.py` immediately before `_create_wechat_backend`:

```python
if config.class_assistant_enabled:
    from .class_assistant.preflight import run_preflight

    hashes = tuple(
        value.strip()
        for value in os.getenv("WCDB_ALLOWED_SHA256", "").split(",")
        if value.strip()
    )
    report = run_preflight(config, PROJECT_ROOT, hashes)
    if not report.ok:
        raise RuntimeError("Class-assistant preflight failed: " + "; ".join(report.errors))
```

- [ ] **Step 10: 回归测试并提交**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\class_assistant -q
.\.venv\Scripts\python.exe -m compileall -q src tests
git add src/class_assistant/preflight.py src/bot.py tests/class_assistant/test_preflight.py scripts/verify_wcdb_runtime.ps1
git add --sparse docs/WCDB_PROVENANCE.md
git commit -m "feat: add WCDB runtime preflight"
git push
```

Expected: 班级助手测试全绿、compileall 退出码 0。

## Checkpoint 4：只读发现稳定群 ID

**Files:**

- Create: `src/class_assistant/group_discovery.py`
- Create: `tests/class_assistant/test_group_discovery.py`
- Modify: `src/wechat/wcdb_backend.py`
- Modify: `src/class_assistant/service.py`
- Modify: `src/web/server.py`
- Modify: `ui/src/components/ClassAssistantPanel.jsx`

- [ ] **Step 1: 写失败测试——发现过程不得调用消息接口**

Create `tests/class_assistant/test_group_discovery.py`:

```python
from src.class_assistant.group_discovery import discover_groups


class FakeClient:
    def get_sessions(self):
        return [
            {"username": "10001@chatroom", "displayName": "班级测试群"},
            {"username": "wxid_private", "displayName": "私聊"},
        ]

    def get_group_members(self, chat_id):
        return [{"username": "a"}, {"username": "b"}]

    def get_messages(self, *args, **kwargs):
        raise AssertionError("group discovery must not read messages")


def test_discovery_returns_only_group_metadata():
    assert discover_groups(FakeClient()) == [
        {"chat_id": "10001@chatroom", "display_name": "班级测试群", "member_count": 2}
    ]
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\class_assistant\test_group_discovery.py -q
```

Expected: FAIL because module does not exist.

- [ ] **Step 3: 实现只读发现函数**

Create `src/class_assistant/group_discovery.py`:

```python
def discover_groups(client):
    groups = []
    for session in client.get_sessions():
        chat_id = str(session.get("username", ""))
        if not chat_id.endswith("@chatroom"):
            continue
        display_name = str(
            session.get("displayName")
            or session.get("display_name")
            or session.get("nickname")
            or chat_id
        )
        members = client.get_group_members(chat_id) or []
        groups.append({
            "chat_id": chat_id,
            "display_name": display_name,
            "member_count": len(members),
        })
    return sorted(groups, key=lambda item: (item["display_name"], item["chat_id"]))
```

- [ ] **Step 4: 通过后端和服务层注入群发现能力**

在 `WcdbBackend` 增加 `discover_group_metadata()`：持有现有客户端锁、确认客户端已打开，然后调用 `discover_groups(self._client)`。在 `ClassAssistantService` 构造函数增加可选的 `group_discoverer: Callable[[], list[dict]]`，并提供 `discover_groups()`；未注入或后端未就绪时返回明确的 503 类错误，不回退到读取消息表。

在 `src/bot.py` 创建服务时注入 `backend.discover_group_metadata`。新增测试必须断言 Web/service 只能得到三个允许字段，且后端未启动时不会读取数据库或消息正文。

- [ ] **Step 5: API 只返回元数据**

在 `src/web/server.py` 增加：

```text
POST /api/class-assistant/groups/discover
```

接口必须复用现有本地 Host/Origin 检查，返回字段只能是 `chat_id`、`display_name`、`member_count`；不得返回消息、成员 wxid、密钥或路径。

- [ ] **Step 6: 控制台增加“发现群”按钮**

在 `ClassAssistantPanel.jsx` 中显示群名、人数、完整 `chat_id` 和“加入白名单”按钮。加入时只更新本地配置草稿，仍需用户点击“保存设置”并重启。

- [ ] **Step 7: 测试、构建并提交**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\class_assistant\test_group_discovery.py tests\class_assistant\test_api.py -q
Set-Location ui
npm run build
Set-Location ..
git add src/class_assistant/group_discovery.py src/wechat/wcdb_backend.py src/class_assistant/service.py src/web/server.py ui/src/components/ClassAssistantPanel.jsx tests/class_assistant/test_group_discovery.py
git commit -m "feat: add metadata-only group discovery"
git push
```

## Checkpoint 5：离线端到端 DRY_RUN 验收

**Files:**

- Modify: `tests/class_assistant/test_service.py`
- Modify: `tests/class_assistant/test_api.py`
- Modify: `docs/CLASS_ASSISTANT_EXECUTION.md`

- [ ] **Step 1: 增加完整工作流测试**

测试必须依次执行：白名单消息采集 → 非白名单拒绝 → 两群独立分析 → 待办和来源保存 → 编辑 → 批准 → 获取 token → DRY_RUN 发送，并断言 sender 调用次数为 0。

- [ ] **Step 2: 增加并发和崩溃恢复测试**

用两个线程同时发送同一批准版本，断言只有一个原子 claim 成功；让 sender 抛异常后断言状态保持 `sending`，再通过 `mark-failed` 转为 `needs_reconciliation`，且不会自动重发。

- [ ] **Step 3: 运行全部专项测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\class_assistant -q
```

Expected: 全部 PASS；不得有 xfail 或 skipped。

- [ ] **Step 4: 验证前端和 Python**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip check
Set-Location ui
npm run build
```

Expected: 三项退出码均为 0。

- [ ] **Step 5: 提交验收测试**

Run:

```powershell
Set-Location C:\Users\27032\WeBot-ClassAssistant
git add tests/class_assistant
git add --sparse docs/CLASS_ASSISTANT_EXECUTION.md
git commit -m "test: cover class assistant dry-run workflow"
git push
```

## Checkpoint 6：测试群只读 48 小时

**Configuration:**

```dotenv
CLASS_ASSISTANT_ENABLED=true
COLLECTION_ENABLED=true
ANALYSIS_ENABLED=false
REVIEW_QUEUE_ENABLED=true
REAL_SEND_ENABLED=false
DRY_RUN=true
TIMEZONE=Asia/Shanghai
DIGEST_SCHEDULE=08:00,20:00
```

启动前必须先在 Checkpoint 4 的控制台中选择唯一测试群并保存，使本地 `CLASS_ASSISTANT_GROUPS` 成为该群的稳定 `chat_id`。不得手写群显示名、示例 ID 或 `*`；若保存后该项仍为空，preflight 必须阻止启动。

- [ ] **Step 1: 使用测试微信账号和测试群启动**

启动前确认控制台 preflight 全绿；不使用日常主账号。测试群中至少包含用户自己的两个测试账号，避免影响老师和同学。

- [ ] **Step 2: 运行 48 小时，只观察采集**

每 12 小时记录一次：采集数量、重复数量、最后游标、数据库大小、错误日志。不得开启分析和发送。

- [ ] **Step 3: 验收只读范围**

使用只统计、不打印正文的命令：

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/messages.db'); print(c.execute('select chat_id,count(*) from captured_messages group by chat_id').fetchall())"
```

Expected: 只出现一个测试群 `chat_id`；私聊和其他群均为 0。

- [ ] **Step 4: Go/No-Go**

Go 条件：48 小时无越界采集、无重复爆增、重启后游标连续、日志没有正文。任一条件失败立即执行控制台“紧急停止”，保持主账号未接入。

## Checkpoint 7：测试群半日分析 48 小时

将 `ANALYSIS_ENABLED=true`，继续保持 `REAL_SEND_ENABLED=false`、`DRY_RUN=true`。

- [ ] **Step 1: 验证 08:00 和 20:00 各执行一次**

Expected: 每个时间槽最多一条 `digest_runs.status=succeeded`；关机错过时只补跑最近一个时间槽。

- [ ] **Step 2: 人工抽检 20 条输出**

逐条检查待办标题、截止时间、置信度、来源消息、群 ID；高风险回复必须处于待编辑状态。

- [ ] **Step 3: 验证失败行为**

临时使用无效模型名触发一次分析失败，然后恢复正确模型。Expected: 失败时间槽不推进分析游标、不产生可发送草稿，JSON 校验最多重试一次。

- [ ] **Step 4: Go/No-Go**

Go 条件：待办准确率至少 90%，来源关联 100%，没有跨群上下文，失败恢复符合预期。

## Checkpoint 8：测试群模拟发送 48 小时

保持：

```dotenv
REAL_SEND_ENABLED=false
DRY_RUN=true
```

- [ ] **Step 1: 编辑并批准至少 10 条草稿**

覆盖普通通知、日期不明确、费用、请假、成绩和隐私内容；高风险草稿必须先编辑才能批准。

- [ ] **Step 2: 对每条草稿执行“确认发送”**

Expected: 完成全部校验并生成 `send_dry_run` 审计事件，但微信窗口没有键盘操作、群中没有新消息。

- [ ] **Step 3: 验证旧版本和重复发送被拒绝**

编辑已批准草稿后尝试发送旧版本；对同一目标和文本重复确认。Expected: 两次均被拒绝。

- [ ] **Step 4: Go/No-Go**

Go 条件：48 小时内 sender 实际调用为 0，审计记录完整，紧急停止即时生效。

## Checkpoint 9：测试群人工批准真实发送

这是首次可能对外产生消息的阶段，必须由用户明确批准后执行。

- [ ] **Step 1: 只对测试群修改开关**

```dotenv
REAL_SEND_ENABLED=true
DRY_RUN=false
```

修改开关前重新打开白名单页面，确认 `CLASS_ASSISTANT_GROUPS` 仍只有 Checkpoint 4 保存的唯一测试群 `chat_id`；页面显示任何其他群时立即停止，不进入真实发送。

- [ ] **Step 2: 发送一条无敏感信息的固定测试文本**

文本固定为：

```text
WeBot 班级事务助手安全发送测试：本消息由用户审核并二次确认。
```

发送前确认页面必须展示完整群名、`chat_id`、版本和全文；微信窗口必须已经位于该测试群。

- [ ] **Step 3: 核对微信和审计记录**

Expected: 群中只出现一条消息；草稿状态为 `sent`；审计记录包含批准版本和发送指纹，不包含聊天原文或 API Key。

- [ ] **Step 4: 执行崩溃对账演练**

在测试群中模拟 sender 抛异常，确认状态为 `sending` 且不自动重发；人工核对群后使用 `mark-failed` 或 `mark-sent` 完成对账。

- [ ] **Step 5: 立即恢复安全开关**

```dotenv
REAL_SEND_ENABLED=false
DRY_RUN=true
```

## Checkpoint 10：日常主账号上线决策

只有 Checkpoint 1–9 全部有证据并通过时才进入本检查点。

- [ ] **Step 1: 重新确认微信版本、DLL 哈希和账号风险**

DLL 哈希必须与 `docs/WCDB_PROVENANCE.md` 一致；微信升级后必须重新跑 preflight 和测试群验证。

- [ ] **Step 2: 正式班级群只读 48 小时**

只把一个正式班级群稳定 `chat_id` 加入白名单；保持 `REAL_SEND_ENABLED=false`、`DRY_RUN=true`。确认没有读取私聊或其他群。

- [ ] **Step 3: 正式群模拟发送 48 小时**

人工审核所有草稿，验证来源、语气、日期和敏感内容；仍不操作微信发送器。

- [ ] **Step 4: 用户作最终选择**

默认结论仍是保持真实发送关闭。只有用户再次明确授权，并接受个人微信主账号的自动化风险，才允许针对单个白名单群开启真实发送；不得使用 `*`，不得自动回复老师或班委私聊。

## 最终验收标准

- Fork 已绑定并推送，工作树干净。
- `npm run build`、`pip check`、`compileall`、班级助手专项测试全部通过。
- WCDB DLL 有可验证来源、SHA-256、扫描结果和兼容版本记录。
- 运行前检查失败时 Bot 不会打开 WCDB 或启动采集线程。
- 群发现不调用消息读取接口，只返回群元数据。
- 测试群只读、分析、模拟发送各运行 48 小时并通过。
- 真实发送只在测试群完成一次人工批准测试，随后恢复关闭。
- 日常主账号在新的明确授权前保持 `REAL_SEND_ENABLED=false`、`DRY_RUN=true`。

## 全局停止条件

遇到以下任一情况立即停止，不进入下一检查点：

- DLL 来源、哈希、签名或 Defender 结果无法确认。
- 微信版本与 WCDB 组件兼容性无法确认。
- 出现私聊或非白名单群采集记录。
- 出现旧 Router 自动回复、错群发送或重复发送。
- API Key、微信凭证或完整聊天正文进入 Git/日志。
- 专项测试、前端构建或 preflight 未通过。
- 用户未明确授权外部 Fork、测试群真实发送或主账号上线。
