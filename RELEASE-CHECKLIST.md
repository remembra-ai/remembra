# Release Checklist

**MANDATORY before ANY deployment (PyPI, NPM, Docker Hub)**

## Pre-Release Verification

### Code Quality
- [ ] All tests pass locally (`pytest tests/ -v`) — expect 263+ passed
- [ ] No test failures or errors
- [ ] Version bumped in ALL manifests:
  - [ ] `pyproject.toml`
  - [ ] `src/remembra/__init__.py`
  - [ ] `sdk/typescript/package.json`
- [ ] CHANGELOG.md updated with new version entry
- [ ] All docs reference correct version (`grep -r "old_version" docs/`)

### Server Smoke Test
- [ ] Server starts without errors (`uv run remembra`)
- [ ] Health check passes (`curl localhost:8787/health`)
- [ ] Core features tested manually:
  - [ ] Store a memory
  - [ ] Recall memories
  - [ ] Forget works
- [ ] New features tested manually (whatever was added this version)

### Quick Start Verification
- [ ] `docker compose -f docker-compose.quickstart.yml up -d` starts all 3 services
- [ ] Qdrant health: `curl localhost:6333/healthz`
- [ ] Ollama health: `curl localhost:11434/api/tags`
- [ ] Remembra health: `curl localhost:8787/health`
- [ ] Store + recall works via curl (no API key needed with quickstart)

### MCP Verification
- [ ] `pip install remembra[mcp]` installs cleanly
- [ ] `remembra-mcp` starts without errors
- [ ] Claude Code: `claude mcp add remembra -- remembra-mcp` connects

## Deploy Only After ALL Checks Pass

```bash
# 1. Run tests
pytest tests/ -v

# 2. Start server and test
uv run remembra &
curl localhost:8787/health

# 3. Manual smoke test
./scripts/mem store "test memory"
./scripts/mem recall "test"

# 4. Build and deploy Python package
python -m build
twine upload dist/*

# 5. Build and push Docker image
docker build -t remembra/remembra:latest -t remembra/remembra:X.Y.Z .
docker push remembra/remembra:latest
docker push remembra/remembra:X.Y.Z

# 6. Publish TypeScript SDK
cd sdk/typescript
npm run build
npm publish

# 7. Deploy documentation
mkdocs build --strict
# (auto-deploys via GitHub Actions on push to main)
```

## Post-Deploy Verification

- [ ] PyPI page shows correct version (`pip install remembra==X.Y.Z`)
- [ ] NPM page shows correct version (`npm info remembra version`)
- [ ] Docker Hub shows new tag (`docker pull remembra/remembra:X.Y.Z`)
- [ ] Fresh install works from each source
- [ ] Documentation site is live with new version
- [ ] Quick start script works from clean machine

---

**NO SHORTCUTS. Test before deploy.**
