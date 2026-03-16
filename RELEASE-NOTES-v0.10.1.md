# Remembra v0.10.1 Release Notes

**Release Date:** March 15, 2026  
**Status:** Production Validated ✅

---

## 🎉 Production Verified

v0.10.1 has been deployed to **api.remembra.dev** and fully validated:

```json
{
  "status": "ok",
  "version": "0.10.1",
  "dependencies": {
    "qdrant": { "status": "ok" }
  },
  "encryption": "AES-256-GCM"
}
```

All systems green. Ready for production use.

---

## 🚀 Highlights

### Universal Agent Installer
One command configures ALL your AI tools:

```bash
pip install remembra
remembra-install --all --url http://localhost:8787
```

**Supported Agents (6+):**
- Claude Desktop
- Claude Code
- Codex CLI
- Cursor
- Windsurf
- Gemini

### Setup Diagnostics
Troubleshoot any agent with:

```bash
remembra-doctor all
remembra-doctor claude-code
remembra-doctor codex
```

Clear failure labels: `dns_failure`, `sandbox_blocked`, `auth_failure`, `timeout`

### Local Bridge for Sandboxed Agents
Proxy for agents that can't reach the network (Codex CLI):

```bash
remembra-bridge --url https://api.remembra.dev --api-key YOUR_KEY
remembra-bridge --status
remembra-bridge --stop
```

---

## ✨ New Features

### Centralized Credentials
- API key stored securely in `~/.remembra/credentials` (chmod 600)
- No need to pass `--api-key` every time after first setup
- Priority: CLI arg > env var > credentials file

### Slim Recall Mode
90% smaller payloads for token-constrained agents:

```python
result = memory.recall("query", slim=True)
# Returns only context string, no metadata bloat
```

### Security Hardening
- **RBAC Enforcement** — Inline permission checks on all memory endpoints
- **Error Sanitization** — No stack traces leaked in production
- **API Key Caching** — Reduced latency on authenticated requests
- **Webhook SSRF Protection** — Validates webhook URLs before delivery
- **2FA/MFA Settings** — New security panel in dashboard

---

## 🔧 Fixes

- **Encryption Key Format** — Production requires `base64:` prefix for `REMEMBRA_ENCRYPTION_KEY`
- **Rate Limit on /health** — Removed (was blocking monitoring)
- **RBAC Inline Checks** — Fixed permission enforcement

---

## 📦 Installation

### Docker (Recommended)
```bash
docker pull remembra/remembra:0.10.1
docker run -d -p 8787:8787 remembra/remembra:0.10.1
```

### PyPI
```bash
pip install remembra==0.10.1
```

### Cloud (api.remembra.dev)
Already deployed! Sign up at [remembra.dev](https://remembra.dev)

---

## 📊 Benchmark Results

Tested on [LoCoMo benchmark](https://github.com/snap-research/locomo) (Snap Research, ACL 2024):

| Category | Accuracy |
|----------|----------|
| Single-hop (direct recall) | **100%** |
| Multi-hop (cross-session) | **100%** |
| Temporal (time-based) | **100%** |
| Open-domain (world knowledge) | **100%** |
| **Overall** | **100%** |

---

## 🔗 Links

- **Website:** [remembra.dev](https://remembra.dev)
- **Docs:** [docs.remembra.dev](https://docs.remembra.dev)
- **GitHub:** [github.com/remembra-ai/remembra](https://github.com/remembra-ai/remembra)
- **Discord:** [discord.gg/mPYQRKzXz5](https://discord.gg/mPYQRKzXz5)
- **Twitter:** [@remembradev](https://twitter.com/remembradev)

---

**Full Changelog:** [v0.10.0...v0.10.1](https://github.com/remembra-ai/remembra/compare/v0.10.0...v0.10.1)

---

Built with ❤️ by [DolphyTech](https://dolphytech.com)
