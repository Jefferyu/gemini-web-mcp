# Step-by-Step Execution Plan: `gemini-web-mcp`

> 配套设计文档：[IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)  
> 项目目标目录：`/Users/ut0pia/agent/gemini-web-mcp`

---

## 新对话一键启动 Prompt（直接复制使用）

### 方式 A：新开单对话（直接选 `Gemini 4 Hydrogen (Medium)` 逐步执行）
```text
请阅读 /Users/ut0pia/agent/gemini-web-mcp/IMPLEMENTATION_PLAN.md 和 /Users/ut0pia/agent/gemini-web-mcp/EXECUTION_STEPS.md，严格按照 EXECUTION_STEPS.md 的 Step 1 -> Step 6 顺序逐步实现并验收。每完成一个 Step 必须运行该步的验收命令通过后再进入下一步。
```

### 方式 B：新开主控对话（主控监督 + 调用 `Hydrogen Medium` Subagent/Swarm）
```text
请作为 Coordinator 阅读 /Users/ut0pia/agent/gemini-web-mcp/IMPLEMENTATION_PLAN.md 与 /Users/ut0pia/agent/gemini-web-mcp/EXECUTION_STEPS.md，调用并监督模型为 gemini-4-hydrogen-medium 的 worker 按步骤落地：
1. 先由单个 worker 完成 Step 1（git init + uv 环境初始化 + 上游签名核对）；
2. Step 2A / 2B / 2C 文件完全正交，可并行分发给不同 worker（使用 workspace:"share" 或明确各自独占文件）；
3. 随后依次完成 Step 3 -> Step 4 -> Step 5 -> Step 6，并由独立 verifier 跑全量检查验收。
```

---

## 0. 已探明的本机基线与全局红线

1. **本机环境现状**：
   - `/opt/homebrew/bin/brew` 已安装；系统自带 `/usr/bin/python3` 为 `3.9.6`；**尚未安装 `uv` 与 Python 3.11+**。
   - `/Users/ut0pia/agent/gemini-web-mcp` 目前**尚未初始化 Git 仓库**（若后续使用多 Agent `workspace: "share"`，必须在 Step 1 先完成 `git init` 与初始提交）。
2. **沙盒与网络最小提权原则**：
   - 仅 `brew install uv`、`uv python install 3.12`、`uv sync --extra dev` 以及真机连网验证需要脱离沙盒访问网络（`BypassSandbox: true`），务必拆分为独立单条命令执行；
   - 其余所有代码编写、`ruff`、`mypy`、`pytest`、子进程 `stdio` 握手自检均在默认沙盒内运行。
3. **三条编码铁律**：
   - **`stdout` 零污染**：MCP 采用 `stdio` 传输，`server.py` 启动最顶部必须 `logger.remove()` 并将 `loguru` / `logging` 强制绑定到 `sys.stderr`，任何一行非 JSON-RPC 输出落入 `stdout` 即视为失败。
   - **禁止猜测上游逆向库签名**：Step 1 装好依赖后，必须先用 `uv run python -c "import inspect, gemini_webapi ..."` 核对实际安装的 `gemini-webapi` 2.x 参数签名，再写 `cookie_backend.py`。
   - **严禁覆盖用户已有全局配置**：本机 `~/.gemini/config/mcp_config.json` 中已存在其他 MCP Server（如 `fast-note-sync`），Step 6 注册时**必须读取原 JSON 并在 `mcpServers` 下增量合并写入**，严禁整文件覆盖。

---

## Step 1：项目脚手架、Git 初始化与 `uv` 环境就绪

### 独占文件
- `/Users/ut0pia/agent/gemini-web-mcp/pyproject.toml`
- `/Users/ut0pia/agent/gemini-web-mcp/.gitignore`
- `/Users/ut0pia/agent/gemini-web-mcp/LICENSE`
- `/Users/ut0pia/agent/gemini-web-mcp/src/gemini_web_mcp/__init__.py`
- `/Users/ut0pia/agent/gemini-web-mcp/src/gemini_web_mcp/backends/__init__.py`
- `/Users/ut0pia/agent/gemini-web-mcp/tests/__init__.py`

### 执行清单
1. 在项目目录执行 `git init`。
2. 创建 `.gitignore`：忽略 `.venv/`、`__pycache__/`、`*.pyc`、`dist/`、`cookies.json`、`*.db`、`*.db-wal`、`*.db-shm`、`.env`、`.env.*`、`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/`。
3. 创建 `LICENSE`（完整 GNU AGPLv3 文本）与包骨架目录 `src/gemini_web_mcp/`（`src` 布局，配置 `[build-system]` 使用 `hatchling` 或 `setuptools`）。
4. 创建 `pyproject.toml`：
   - `requires-python = ">=3.11"`
   - `[project.scripts]`：`gemini-web-mcp = "gemini_web_mcp.server:main"`
   - 核心依赖：`mcp>=1.2.0`、`gemini-webapi>=2.1.0,<3.0.0`、`pydantic>=2.0`、`pydantic-settings>=2.0`
   - 可选依赖：`browser-cookies = ["gemini-webapi[browser]"]`，`dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "ruff>=0.6", "mypy>=1.11"]`
   - 配置 `[tool.pytest.ini_options]`（`asyncio_mode = "auto"`）、`[tool.ruff]`、`[tool.mypy]`。
5. 安装 `uv` 并拉起 Python 3.12 开发环境（需网络权限，单条拆分执行）：
   - `/opt/homebrew/bin/brew install uv`
   - `uv sync --python 3.12 --extra dev --extra browser-cookies`
6. **核对上游 API 真实签名**（把结果记录或用于指导 Step 3）：
   ```bash
   uv run python -c "
   import inspect, gemini_webapi
   from gemini_webapi import GeminiClient
   print('version:', getattr(gemini_webapi, '__version__', 'unknown'))
   print('GeminiClient.__init__:', inspect.signature(GeminiClient.__init__))
   print('GeminiClient.init:', inspect.signature(GeminiClient.init))
   print('GeminiClient.start_chat:', inspect.signature(GeminiClient.start_chat))
   print('GeminiClient.generate_content:', inspect.signature(GeminiClient.generate_content))
   "
   ```
7. 提交初始脚手架 commit（方便后续回滚或并行切分支）。

### 本步验收门禁（Done Gate）
- `uv run python --version` 输出 `Python 3.12.x`；
- 上述 `inspect.signature` 命令退出码为 `0` 且成功打印 `gemini_webapi` 2.x 真实签名。

---

## Step 2：核心基础模块实现（无外部网络依赖，2A / 2B / 2C 可并行）

### Step 2A：配置管理与多级鉴权加载器
- **独占文件**：
  - `src/gemini_web_mcp/config.py`
  - `src/gemini_web_mcp/auth.py`
  - `tests/test_auth.py`
- **实现要点**：
  1. `Settings(BaseSettings)`（环境变量前缀 `GEMINI_WEB_MCP_`）：
     - `backend: str = "cookie"`
     - `default_model: str = "unspecified"`
     - `proxy: str | None = None`（若未显式设置 `GEMINI_WEB_MCP_PROXY`，自动回退读取环境变量 `HTTPS_PROXY` / `https_proxy` / `HTTP_PROXY` / `http_proxy` / `ALL_PROXY` / `all_proxy`）
     - `cookies_path: Path = Path("~/.config/gemini-web-mcp/cookies.json").expanduser()`
     - `sessions_db_path: Path = Path("~/.config/gemini-web-mcp/sessions.db").expanduser()`
     - `timeout_seconds: int = 120`、`thinking_timeout_seconds: int = 300`
     - `max_files_per_request: int = 10`、`max_file_size_mb: int = 20`
     - `log_level: str = "WARNING"`
  2. `AuthLoader`：
     - 支持按优先级提取凭据：`GEMINI_SECURE_1PSID` (+ 可选 `GEMINI_SECURE_1PSIDTS`) -> `cookies.json`（同时兼容 `{"__Secure-1PSID": "..."}` 字典格式与 EditThisCookie/Cookie-Editor 导出的 `[ {"name": "__Secure-1PSID", "value": "..."}, ... ]` 数组格式）-> 可选 `browser-cookie3` 回退。
     - **防旧 `1PSIDTS` 覆盖机制**：检查上游 `gemini-webapi` 本地缓存目录中是否已有更新的轮换 `1PSIDTS`（或提供“首次仅用新配置 / 鉴权报错时清除过期缓存重试”的辅助函数），并在缺失 Cookie 或 macOS Keychain 权限受阻时抛出带中文/英文操作指引的自定义异常（绝不在异常文本中打印完整 Cookie 明文）。
- **验收命令**：`uv run pytest tests/test_auth.py -v`

### Step 2B：SQLite WAL 多进程安全会话存储
- **独占文件**：
  - `src/gemini_web_mcp/sessions.py`
  - `tests/test_sessions.py`
- **实现要点**：
  1. 定义 `SessionRecord`（Pydantic 模型或 dataclass）：
     - `label: str`
     - `metadata: list[str | None]`（使用 JSON 序列化存入 SQLite `TEXT` 列，**完整保留 3 个或 4+ 个元素**，严禁截断）
     - `model: str`
     - `turn_count: int`
     - `created_at: str`、`updated_at: str`（ISO 8601 UTC）
  2. `SessionStore`：
     - 自动创建父目录（权限 `0o700`），连接 SQLite 时开启 `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`。
     - 提供 `get(label)`、`upsert(record)`、`list_sessions()`、`delete(label) -> bool`、`clear() -> int`。
     - 维护进程内 `label -> asyncio.Lock` 锁池（暴露 `@asynccontextmanager async def lock(self, label: str)`），确保同标签请求串行、异标签请求并行。
- **验收命令**：`uv run pytest tests/test_sessions.py -v`（必须包含 4 元素 `metadata` 无损测试 + 两个独立 `SessionStore` 实例并发交替写入同一 `sessions.db` 不丢数据的测试）。

### Step 2C：本地附件安全与体积边界校验
- **独占文件**：
  - `src/gemini_web_mcp/files.py`
  - `tests/test_files.py`
- **实现要点**：
  1. 实现 `validate_upload_files(files: Sequence[str | Path], *, max_count: int = 10, max_size_mb: int = 20, extra_blocked_paths: Sequence[Path] = ()) -> list[Path]`：
     - 数量超限直接报清晰错误；
     - `Path(f).expanduser().resolve()` 解析真实路径（防软链绕过）；
     - 校验存在且 `is_file()`（拒绝目录、设备文件、不存在路径）；
     - 校验字节大小 `<= max_size_mb * 1024 * 1024`；
     - 敏感路径黑名单拦截：拒绝位于 `~/.ssh`、`~/.aws`、`~/.gnupg` 下的任何文件，拒绝文件名为 `.env` 或匹配 `.env.*`（不含 `.env.example`）的文件，拒绝传入的 `cookies.json` / `sessions.db` 自身路径。
- **验收命令**：`uv run pytest tests/test_files.py -v`

---

## Step 3：后端抽象与 `CookieBackend` 实现

### 独占文件
- `src/gemini_web_mcp/backends/base.py`
- `src/gemini_web_mcp/backends/cookie_backend.py`
- `tests/test_cookie_backend.py`

### 执行清单
1. 在 `backends/base.py` 定义统一模型与抽象基类 `GeminiBackend(ABC)`：
   - `AskRequest(prompt, metadata, model, files, include_thoughts, timeout_seconds)`
   - `AskResponse(text, thoughts, metadata, resolved_model)`
   - `HealthStatus(ok, backend, auth_source, proxy_configured, proxy_masked, available_models, message)`
   - 抽象方法：`init()`, `ask(req)`, `health()`, `close()`。
2. 基于 Step 1 核对出的 `gemini-webapi` 2.x 真实签名实现 `CookieBackend`：
   - **Double-Checked `asyncio.Lock` 懒加载**：MCP 启动时不联网；首次 `ask` / `health` 时在锁内执行 `await client.init(timeout=..., auto_close=False, auto_refresh=True)`，传入 `proxy`。
   - **模型解析**：支持 `flash` / `pro` / `thinking` / `unspecified` 语义别名（结合 `gemini-webapi` 2.x 的模型枚举或 `resolve_model`），同时允许透传原始模型标识；若模型属于 `thinking` 类，自动采用 `thinking_timeout_seconds`（300s）。
   - **异常友好包装**：捕获上游 `AuthError`、超时、网络/代理错误与频率风控限制，转换为结构化可读诊断信息（如提示检查 `GEMINI_WEB_MCP_PROXY` 或更新 Cookie），不抛裸 Traceback。
3. 编写 `tests/test_cookie_backend.py`（Mock `GeminiClient`）：覆盖并发调用只触发一次 `client.init()`、别名映射、`thoughts` 提取、`AuthError` 友好提示与 `close()` 清理。

### 本步验收门禁（Done Gate）
- `uv run pytest tests/test_cookie_backend.py -v` 全部通过。

---

## Step 4：MCP `stdio` Server 组装与协议纯净度验证

### 独占文件
- `src/gemini_web_mcp/server.py`
- `tests/test_server.py`

### 执行清单
1. **`stdout` 强隔离（置于 `server.py` 文件最顶部，先于业务与第三方导入）**：
   - 调用 `loguru.logger.remove()` 并绑定 `sys.stderr`（级别读取 `GEMINI_WEB_MCP_LOG_LEVEL`，默认 `WARNING`）；同步配置标准库 `logging.basicConfig(stream=sys.stderr, ...)`。
2. 使用 `FastMCP("gemini-web-mcp", lifespan=...)` 注册 3 个工具与 `ToolAnnotations`：
   - `gemini_ask(prompt: str, session: str = "default", new_chat: bool = False, model: str | None = None, files: list[str] | None = None, include_thoughts: bool = False)`：
     - 使用 `async with session_store.lock(session):` 保护同标签多轮状态；
     - **续聊模型一致性保护**：若 `not new_chat` 且本地已存在 `record`：
       - `model is None` 时自动沿用 `record.model`；
       - `model is not None` 且规范化后与 `record.model` 不一致时，**直接拒绝发请求并返回明确提示**（告知当前会话绑定模型为 `record.model`，如需切换模型请传 `new_chat=True` 或换用新的 `session` 标签），避免触发 Google 后端 `1050` 错误；
     - 请求执行期间启动后台协程，每 10 秒通过 MCP `Context.report_progress` 发送进度心跳（请求结束立即 `cancel`）；
     - 若 `include_thoughts=True` 且响应含 `thoughts`，以折叠块或清晰分段置于正文前；
     - 正文末尾固定附加状态行：`\n\n---\n[session: <session> | turn: <turn_count> | model: <resolved_model>]`。
   - `gemini_sessions(action: Literal["list", "get", "delete", "clear"] = "list", session: str | None = None)`：管理本地 SQLite 会话。
   - `gemini_health()`（`readOnlyHint=True`）：返回后端健康状态、脱敏代理信息（如 `http://127.0.0.1:7890` 隐去账密）、凭据来源与可用模型列表。
3. 编写 `tests/test_server.py`：
   - 使用 Mock Backend 测试新建会话、同标签多轮续聊、跨模型拦截提示、`new_chat=True` 重置、附件校验联动、`gemini_sessions` 全动作与 `gemini_health`；
   - **子进程 `stdio` 端到端协议测试**：通过 `asyncio.create_subprocess_exec("uv", "run", "gemini-web-mcp", ...)` 发送真实 MCP `initialize` 与 `tools/list` JSON-RPC 消息，断言 `stdout` 每一行都是合法 JSON 且成功返回 3 个工具定义！

### 本步验收门禁（Done Gate）
```bash
uv run ruff check src tests
uv run mypy src
uv run pytest -v
```
上述三条命令必须全部零报错通过。

---

## Step 5：开源文档、CI 工作流与路由模板

### 独占文件
- `.github/workflows/ci.yml`
- `examples/skills/gemini-web/SKILL.md`
- `examples/GEMINI.md`
- `README.md`

### 执行清单
1. 编写 `.github/workflows/ci.yml`：在 `ubuntu-latest` + Python `["3.11", "3.12", "3.13"]` 矩阵上安装 `uv`，执行 `ruff check`、`mypy src` 与 `pytest -v`。
2. 编写 `examples/skills/gemini-web/SKILL.md`（带标准 YAML frontmatter `name` / `description`）：说明何时把任务外包给 `gemini_ask`（架构第二视角、跨文件 Bug 会诊、长日志/PDF/截图多模态分析）、会话标签命名规范、续聊不混用模型的注意事项。
3. 编写 `examples/GEMINI.md`：供项目级 `GEMINI.md` / `AGENTS.md` 或 Claude Code / Gemini CLI 复制的精简路由规则。
4. 编写中英双语 `README.md`：包含安装运行方式（`uv --directory` / `uvx --from git+...`）、macOS GUI 客户端 `GEMINI_WEB_MCP_PROXY` 必填提示、Cookie 获取与独立账号建议、Antigravity / Claude Code / Cursor 配置示例与 AGPL-3.0 免责声明。

### 本步验收门禁（Done Gate）
- 检查 Markdown 链接与 JSON 配置示例语法全部合法；再次运行 `uv run ruff check src tests && uv run pytest -v` 确保无回归。

---

## Step 6：本机 Antigravity 接入与真机收尾验收

### 执行清单
1. **增量合并注册 MCP 配置（严禁覆盖已有配置）**：
   - 先读取 `~/.gemini/config/mcp_config.json` 现有内容，保留其中已有的全部 `mcpServers`（例如 `fast-note-sync`）；
   - 在 `mcpServers` 下新增 `"gemini-web-mcp"` 条目：
     ```json
     "gemini-web-mcp": {
       "command": "uv",
       "args": [
         "--directory",
         "/Users/ut0pia/agent/gemini-web-mcp",
         "run",
         "gemini-web-mcp"
       ],
       "env": {
         "GEMINI_WEB_MCP_LOG_LEVEL": "WARNING"
       }
     }
     ```
     *(注：若本机访问 Google 需要代理，先检测常见本地代理端口或环境变量，按需写入 `"GEMINI_WEB_MCP_PROXY"`。)*
2. **连通性自检**：
   - 检查本机是否已提供 Cookie 或可用浏览器登录态：
     - 若就绪：运行一次真实 `health()` 与同一 `session` 下的两轮 `ask()` 验证多轮上下文记忆，随后清理测试会话；
     - 若需用户提供 Cookie：输出清晰的 `~/.config/gemini-web-mcp/cookies.json` 填写模板与验证命令。
3. **Git 仓库收尾检查**：
   - 运行 `git status`，确保 `cookies.json`、`*.db`、`.venv/` 均未被追踪，提交干净的完成态 Commit。
