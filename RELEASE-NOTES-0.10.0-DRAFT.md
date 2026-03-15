# Remembra v0.10.0 Release Notes (DRAFT)

> **Status:** DRAFT — awaiting final features from Codex build

## 🚀 Highlights

### One-Command Agent Setup
```bash
npx remembra setup --all
```
Auto-detects and configures ALL your AI agents in one shot:
- Claude Desktop
- Claude Code
- Codex CLI
- Gemini
- Cursor
- Windsurf

No manual config editing. No JSON wrangling. Just run and go.

---

## ✨ New Features

### `npx remembra setup` CLI (NEW)
- **Auto-detection:** Scans for installed AI agents
- **Safe merging:** Preserves existing MCP configurations
- **Cross-platform:** macOS and Linux support
- **Credential management:** Centralized `~/.remembra/credentials`

### Security Hardening
- **RBAC Enforcement:** Inline permission checks on all memory endpoints
- **Generic Exception Handler:** Sanitizes error responses (no stack traces leaked)
- **API Key Caching:** Reduced latency on authenticated requests
- **Webhook SSRF Protection:** Validates webhook URLs before delivery
- **2FA/MFA Settings UI:** New security settings panel in dashboard

### Fixes
- Rate limit removed from `/health` endpoint (was blocking monitoring)
- RBAC permissions now properly enforced on memory operations

---

## 📦 Installation

### Upgrade Docker
```bash
docker pull remembra/remembra:0.10.0
docker stop remembra && docker rm remembra
docker run -d -p 8787:8787 -v remembra-data:/app/data remembra/remembra:0.10.0
```

### Upgrade pip
```bash
pip install --upgrade remembra
```

---

## 🔧 Breaking Changes

None — v0.10.0 is backward compatible with v0.9.x.

---

## 📊 What's Next (v0.11.0)

- [ ] Team Spaces with invite links
- [ ] Memory sharing across organizations
- [ ] Dashboard analytics v2

---

**Full Changelog:** [v0.9.0...v0.10.0](https://github.com/remembra-ai/remembra/compare/v0.9.0...v0.10.0)
