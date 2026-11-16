---
name: gemini-web
description: Outsource second-opinion architecture reviews, cross-file bug consultations, and large log/PDF/image multimodal analysis to Web Gemini via `gemini_ask`, `gemini_sessions`, and `gemini_health`.
---

# Web Gemini Subagent Skill (`gemini-web`)

Use `gemini-web-mcp` as an external second-opinion subagent when an independent model perspective or Web Gemini's large-context multimodal capability adds clear value.

## When to Invoke

- **Architecture / Design Second Opinion**: Before committing to a major refactor or complex schema/API design.
- **Stubborn Bug Triage**: When stuck across multiple debugging iterations and you want an outside hypothesis.
- **Large Log / Multimodal Analysis**: Inspecting screenshots, diagrams, PDFs, or long trace logs via `files=[...]`.

## Tool Usage Guidelines

1. **Check connectivity first (if unsure)**: Call `gemini_health` (`readOnlyHint=True`) to confirm authentication, masked proxy, and available models.
2. **Use task-scoped `session` labels**:
   - Name sessions descriptively, e.g., `arch-auth-v2`, `bug-wal-lock`, `ui-screenshot-review`.
   - Reuse the same `session` label for follow-up turns so Gemini retains server-side conversation context.
3. **Never switch models mid-session**:
   - Omit `model` on follow-up turns to automatically inherit the session's bound model.
   - Switching models within an existing session triggers a protective refusal (to prevent Gemini error `1050`). Pass `new_chat=True` or use a new `session` label if you need another model (`flash`, `pro`, `thinking`).
4. **Package context concisely**:
   - Include only the relevant file snippets, constraints, error messages, and specific questions.
   - For multimodal/file inspection, pass local file paths via `files=[...]` (note: `.env`, `~/.ssh`, `~/.aws`, `~/.gnupg`, `cookies.json`, and `sessions.db` are blocked by security policy).
5. **Thinking mode**:
   - Use `model="thinking"` and `include_thoughts=True` only for deep reasoning tasks; timeout automatically expands to 300s with progress heartbeats.
6. **Clean up temporary sessions**:
   - Use `gemini_sessions(action="list")` / `delete` / `clear` when finished with throwaway consultations.
