# Remembra v0.8.0 — Complete Handoff Document

**Date:** March 7, 2026
**From:** Claude Code session (Remembra project)
**For:** Clawbot / any agent continuing this work
**Launch:** March 11, 2026

---

## CURRENT STATE

### What Has Been Pushed to GitHub (commit `445124e` + `b45ba47`)

The v0.8.0 release is **live on PyPI and NPM**. The following was already committed, pushed, and published:

| Target | Status | Details |
|--------|--------|---------|
| **GitHub** | ✅ Pushed | Tag `v0.8.0` on `main` at `remembra-ai/remembra` |
| **PyPI** | ✅ Live | `pip install remembra==0.8.0` works |
| **NPM** | ✅ Live | `npm install remembra@0.8.0` works |
| **Docker Hub** | ⚠️ Partial | Built and pushed to `nqandai/remembra:0.8.0` + `:latest`. The `remembra` Docker Hub org does NOT exist yet — needs to be created before launch, then images re-pushed to `remembra/remembra:0.8.0` |

### Code Changes in v0.8.0 (already pushed)

**40 files changed, +3,233 / -296 lines across 5 workstreams:**

1. **httpx Connection Reuse** — Persistent clients in all 6 embedding providers (`embeddings.py`), webhook delivery, Python SDK (`client/memory.py`), MCP server. Eliminates 100-300ms TCP+TLS overhead per operation.

2. **Multi-Provider Entity Extraction** — Added `AnthropicEntityExtractor` and `OllamaEntityExtractor` to `extraction/entities.py` plus `create_entity_extractor()` factory. Configurable via `REMEMBRA_LLM_PROVIDER` env var.

3. **Usage Warning Banners** — `cloud/limits.py` now returns `X-Remembra-Usage-Percent` headers and includes `usage_warning` in API responses at 60/80/95% thresholds. `models/memory.py` has new `UsageWarning` model.

4. **Quick Start DX** — New `quickstart.sh` (curl|bash install), new `docker-compose.quickstart.yml` (zero-config with Qdrant + Ollama + Remembra).

5. **125 New Tests** — `tests/conftest.py`, `test_embeddings.py`, `test_entities.py`, `test_conflicts.py`, `test_spaces.py`, `test_plugins.py`. Total: 263 passed, 0 failures.

---

## WHAT STILL NEEDS TO BE COMMITTED AND PUSHED

**18 files are modified but NOT yet committed.** These are consistency fixes from a post-deployment audit:

### Files Modified (uncommitted):

```
DOCKER.md                            — GitHub URL fix
Dockerfile                           — GitHub URL fix in label
README.md                            — "What's New" section updated from v0.7.0 to v0.8.0
demo/DEMO_SCRIPT.md                  — GitHub URL fix (was anthropics/remembra)
demo/demo_recording.py               — GitHub URL fix (was YOUR_ORG/remembra)
docs/TROUBLESHOOTING.md              — Version 0.7.1→0.8.0 in example JSON + GitHub URL fix
docs/getting-started/docker.md       — GitHub URL fix (was remembra-dev/remembra)
docs/getting-started/installation.md — GitHub URL fix (was remembra/remembra)
docs/getting-started/quickstart.md   — GitHub URL fix (was remembra/remembra)
docs/index.md                        — GitHub URL fix (was AskDolphy) + broken relative link fix
docs/reference/changelog.md          — v0.8.0 content was showing v0.7.1 changes; replaced with actual v0.8.0 content, added v0.7.2 entry
landing/index.html                   — v0.8.0 changelog card added, badge "NEW in v0.7.2"→"v0.8.0", license Apache 2.0→MIT
landing/terms.html                   — License Apache 2.0→MIT (6 occurrences)
pyproject.toml                       — GitHub URLs AskDolphy→remembra-ai (5 project URLs)
sdk/typescript/package-lock.json     — Version 0.7.1→0.8.0
server.json                          — Version 0.6.7→0.8.0 (MCP registry)
```

### Files Deleted (uncommitted):
```
landing/index-new.html               — Unused draft
landing/index-v2.html                — Unused draft
```

### What Each Fix Category Covers:

**1. GitHub org URL standardization (11 files)**
All GitHub URLs standardized to `github.com/remembra-ai/remembra`. Previously used 7 different orgs:
- `AskDolphy/remembra` (pyproject.toml, docs/index.md)
- `remembra/remembra` (Dockerfile, DOCKER.md, installation.md, quickstart.md)
- `remembra-dev/remembra` (docker.md)
- `remembradev/remembra` (TROUBLESHOOTING.md)
- `anthropics/remembra` (demo/DEMO_SCRIPT.md)
- `YOUR_ORG/remembra` (demo/demo_recording.py)
- `dolphy/remembra` (quickstart.sh — already correct in pushed version)

**2. Stale version references (4 files)**
- `server.json`: 0.6.7 → 0.8.0
- `sdk/typescript/package-lock.json`: 0.7.1 → 0.8.0
- `docs/TROUBLESHOOTING.md`: example JSON 0.7.1 → 0.8.0
- `README.md`: "What's New in v0.7.0" → "What's New in v0.8.0" with correct features

**3. Landing page fixes (2 files)**
- `landing/index.html`:
  - Badge: "NEW in v0.7.2" → "NEW in v0.8.0"
  - License: "Apache 2.0" → "MIT" (2 locations)
  - Added v0.8.0 changelog card as first item in changelog section
- `landing/terms.html`:
  - All "Apache 2.0" / "Apache License 2.0" → "MIT" (6 occurrences)

**4. Documentation fixes (3 files)**
- `docs/reference/changelog.md`: Body content under v0.8.0 header was showing v0.7.1 changes. Replaced with actual v0.8.0 content. Added v0.7.2 entry.
- `docs/index.md`: Fixed 2 broken relative links (`docs/TROUBLESHOOTING.md` → `TROUBLESHOOTING.md`, `docs/SETUP-CHECKLIST.md` → `SETUP-CHECKLIST.md`)

---

## TO DO: Commit and Push These Fixes

```bash
cd /Users/dolphy/projects/remembra

# Stage all modified files
git add DOCKER.md Dockerfile README.md \
  demo/DEMO_SCRIPT.md demo/demo_recording.py \
  docs/TROUBLESHOOTING.md docs/getting-started/docker.md \
  docs/getting-started/installation.md docs/getting-started/quickstart.md \
  docs/index.md docs/reference/changelog.md \
  landing/index.html landing/terms.html \
  pyproject.toml sdk/typescript/package-lock.json server.json

# Remove deleted draft files
git rm landing/index-new.html landing/index-v2.html 2>/dev/null || true

# Commit
git commit -m "fix: standardize GitHub URLs, fix stale versions, correct license to MIT, add v0.8.0 changelog entries"

# Push
git push origin main
```

---

## TO DO: Docker Hub Organization

The `remembra` Docker Hub organization does **not exist**. Images were pushed to `nqandai/remembra` as a stopgap.

**Before launch:**
1. Create the `remembra` organization on Docker Hub at https://hub.docker.com/orgs
2. Re-tag and push:
   ```bash
   docker tag nqandai/remembra:0.8.0 remembra/remembra:0.8.0
   docker tag nqandai/remembra:latest remembra/remembra:latest
   docker push remembra/remembra:0.8.0
   docker push remembra/remembra:latest
   ```

All docs reference `remembra/remembra` — this is intentional. Once the org exists and images are pushed, everything will resolve.

---

## TO DO: Host quickstart.sh

The `curl -sSL https://get.remembra.dev/quickstart.sh | bash` URL is referenced in 9+ files but the URL does not exist yet.

**Options:**
1. Set up `get.remembra.dev` as a redirect/static host serving `quickstart.sh`
2. Or use the raw GitHub URL: `https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh`
3. The script itself (`quickstart.sh` in repo root) downloads `docker-compose.quickstart.yml` from `https://raw.githubusercontent.com/remembra-ai/remembra/main/docker-compose.quickstart.yml`

---

## TO DO: Landing Page Updates Still Needed

The landing page at `landing/index.html` has been partially fixed (license, version badge, changelog card). But the following may still need attention:

1. **landing/changelog.html** — Missing v0.7.2 and v0.8.0 entries entirely. Only has v0.7.0 and v0.7.1.
2. **landing/index-backup-*.html** files — These are backups/drafts with old versions. Consider deleting them to avoid confusion.
3. **The hero section / main CTA** — Verify it references the latest install method and version.

---

## TO DO: Documentation Site Rebuild

If the docs are served via MkDocs + GitHub Pages or Vercel:
- The push to `main` should trigger an auto-rebuild
- Verify at https://remembra.dev (or wherever docs are hosted) that v0.8.0 content is live
- Check that the new quickstart section appears correctly

---

## KNOWN REMAINING ISSUES (Low Priority)

1. **BUILD-BLUEPRINT.md line 13** — Says "0.7.0 Alpha". This is an internal planning doc; update if desired.
2. **README-polished.md** — Has "What's New in v0.7.0". This appears to be a draft/alternate README. Update or delete.
3. **REMEMBRA-DEEP-RESEARCH-REPORT.md** — Internal research doc references old Docker image name. Low priority.
4. **WHY-WE-WIN.md, PRODUCT-SPEC.md, ARCHITECTURE.md** — Internal strategy docs reference `remembra/remembra` Docker image (which is correct since the org will be created).
5. **landing/hero-demo-*.html** files — Iterative drafts of hero demo. Consider cleaning up.
6. **`.zshenv` cargo error** — Every shell command shows `/Users/dolphy/.zshenv:.:1: no such file or directory: /Users/dolphy/.cargo/env`. This is a cosmetic issue in the user's shell config — not related to Remembra.

---

## CLAWBOT FIX (Completed)

The Clawbot gateway was stuck in an infinite error loop due to a corrupted session. Two fixes were applied:

### Session Repair
- Corrupted session `6a1f7dec-b59b-4507-86f8-53021431ad6c` had 39 messages removed (lines 326-364)
- Backup at `.corrupted.bak`
- Root cause: Claude API returned `overloaded_error` mid-stream while streaming a `tool_use` block, leaving truncated `partialJson` in the session

### Permanent Code Fix (2 files patched)
**`/Users/dolphy/.npm-global/lib/node_modules/clawdbot/dist/agents/session-transcript-repair.js`**
- Added `isValidToolCallArguments()` — checks for `partialJson` field and validates arguments
- Added `stripPartialToolCalls()` — removes partial tool call blocks from assistant messages before API calls
- Updated `extractToolCallsFromAssistant()` — skips invalid tool calls so no synthetic `tool_result` is created

**`/Users/dolphy/.npm-global/lib/node_modules/clawdbot/dist/agents/session-tool-result-guard.js`**
- Added same `isValidToolCallArguments()` check to the real-time guard
- Prevents synthetic tool results from being created for partial tool calls at write time

**What happens now when the API errors mid-stream:**
1. Partial tool call gets saved to session (can't prevent)
2. On next API call, `stripPartialToolCalls()` removes the malformed block
3. `extractToolCallsFromAssistant()` skips it, no synthetic `tool_result` created
4. API receives clean messages → no more infinite error loop

**Note:** These patches are to the globally installed npm package (`~/.npm-global/lib/node_modules/clawdbot/dist/`). They will be overwritten on the next `npm update -g clawdbot`. Consider upstreaming the fix.

---

## TEST SUITE STATUS

```
263 passed, 1 skipped, 0 failures (3.82s)
```

Test files:
- `tests/test_main.py` — API smoke tests
- `tests/test_client.py` — Python SDK
- `tests/test_conversation_ingest.py` — Ingestion
- `tests/test_temporal.py` — Temporal decay
- `tests/test_security.py` — Auth & security
- `tests/test_retrieval.py` — Hybrid search & ranking
- `tests/conftest.py` — Shared fixtures (NEW)
- `tests/test_embeddings.py` — All 6 embedding providers (NEW)
- `tests/test_entities.py` — Entity extraction + factory (NEW)
- `tests/test_conflicts.py` — Conflict resolution (NEW)
- `tests/test_spaces.py` — Memory spaces + RBAC (NEW)
- `tests/test_plugins.py` — Plugin system (NEW)

---

## COMPLETE FILE INVENTORY

### Source Files Modified (already pushed in v0.8.0):
```
src/remembra/__init__.py              — version 0.8.0
src/remembra/api/v1/memories.py       — usage warning in responses
src/remembra/client/memory.py         — persistent httpx client + context manager
src/remembra/cloud/limits.py          — usage warning thresholds + headers
src/remembra/config.py                — ollama_base_url config
src/remembra/extraction/entities.py   — Anthropic + Ollama extractors + factory
src/remembra/main.py                  — embeddings.close() in lifespan cleanup
src/remembra/mcp/server.py            — persistent httpx client for ingestion
src/remembra/models/memory.py         — UsageWarning model
src/remembra/services/memory.py       — use extractor factory
src/remembra/storage/embeddings.py    — persistent httpx clients in all 6 providers
src/remembra/webhooks/delivery.py     — persistent httpx client
```

### Config/Build Files Modified (already pushed):
```
pyproject.toml                        — version 0.8.0 + anthropic optional dep
sdk/typescript/package.json           — version 0.8.0
.env.example                          — new provider env vars
uv.lock                               — dependency lock update
docker-compose.quickstart.yml         — NEW: zero-config compose
quickstart.sh                         — NEW: curl|bash installer
```

### Test Files (already pushed):
```
tests/conftest.py                     — NEW: shared fixtures
tests/test_embeddings.py              — NEW: 6 provider tests
tests/test_entities.py                — NEW: extraction + factory tests
tests/test_conflicts.py               — NEW: conflict resolution tests
tests/test_spaces.py                  — NEW: spaces + RBAC tests
tests/test_plugins.py                 — NEW: plugin system tests
tests/test_client.py                  — version assertion fix
```

### Docs/Marketing Modified (already pushed):
```
CHANGELOG.md                          — v0.8.0 entry
README.md                             — quick start section
RELEASE-CHECKLIST.md                  — expanded deploy steps
docs/getting-started/quickstart.md    — zero-config section
docs/getting-started/installation.md  — providers + quickstart
docs/getting-started/docker.md        — zero-config, compose profiles
docs/guides/entity-resolution.md      — multi-provider extraction
docs/guides/conversation-ingestion.md — version ref
docs/guides/rest-api.md               — version ref
docs/index.md                         — v0.8.0 landing
docs/integrations/mcp-server.md       — v0.8.0 features
docs/reference/changelog.md           — version ref
docs/DEPLOYMENT.md                    — version ref
docs/TROUBLESHOOTING.md               — version ref
landing/index.html                    — v0.8.0 features + design updates
```

### Files Modified (NOT YET COMMITTED — need to be pushed):
```
DOCKER.md                             — GitHub URL fix
Dockerfile                            — GitHub URL fix in label
README.md                             — "What's New" v0.7.0→v0.8.0
demo/DEMO_SCRIPT.md                   — GitHub URL fix
demo/demo_recording.py                — GitHub URL fix
docs/TROUBLESHOOTING.md               — version + GitHub URL fix
docs/getting-started/docker.md        — GitHub URL fix
docs/getting-started/installation.md  — GitHub URL fix
docs/getting-started/quickstart.md    — GitHub URL fix
docs/index.md                         — GitHub URL + broken link fix
docs/reference/changelog.md           — v0.8.0 content fix + v0.7.2 entry
landing/index.html                    — v0.8.0 card, badge, license fix
landing/terms.html                    — license Apache→MIT
pyproject.toml                        — GitHub URLs in project metadata
sdk/typescript/package-lock.json      — version 0.8.0
server.json                           — version 0.8.0
```

### Files to Delete (NOT YET COMMITTED):
```
landing/index-new.html                — unused draft
landing/index-v2.html                 — unused draft
```

---

## VERIFICATION CHECKLIST (Post-Push)

- [ ] `pip install remembra==0.8.0` works → ✅ already verified
- [ ] `npm install remembra@0.8.0` works → ✅ already verified
- [ ] `docker pull remembra/remembra:0.8.0` works → ❌ needs Docker Hub org creation
- [ ] `curl -sSL https://get.remembra.dev/quickstart.sh | bash` works → ❌ needs DNS/hosting setup
- [ ] `pytest tests/ -v` → 263 passed ✅
- [ ] remembra.dev docs show v0.8.0 → verify after push
- [ ] Landing page shows v0.8.0 changelog card → verify after push
- [ ] Landing page license says MIT → verify after push
- [ ] All GitHub links resolve to `remembra-ai/remembra` → verify after push
