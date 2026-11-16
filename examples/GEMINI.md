# External Gemini Web Subagent Routing (`gemini-web-mcp`)

When working in this workspace, you have access to the `gemini-web-mcp` MCP server (`gemini_ask`, `gemini_sessions`, `gemini_health`).

## Routing Rules

- **Do not** invoke `gemini_ask` for trivial edits, single-line syntax questions, or routine local file searches.
- **Do** invoke `gemini_ask` when:
  1. You need an independent second-opinion review on architecture, security boundaries, or tricky concurrency bugs.
  2. You need multimodal analysis of screenshots, PDFs, or large log files (`files=[...]`).
- **Session Hygiene**:
  - Always pass a descriptive `session` label (e.g., `review-pr-42` or `bug-race-cond`) rather than leaving everything in `default`.
  - On follow-up turns in the same `session`, omit `model` so the server preserves the session's bound model. To change models, pass `new_chat=True` or pick a fresh `session` label.
- **Security**:
  - Never attempt to pass secrets, `.env` files, SSH keys, or credential stores to `gemini_ask`.
