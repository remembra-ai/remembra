# Contributing to Remembra

First off, thanks for taking the time to contribute! 🎉

## Ways to Contribute

### 🐛 Bug Reports

Found a bug? [Open an issue](https://github.com/remembra-ai/remembra/issues/new) with:
- Clear title and description
- Steps to reproduce
- Expected vs actual behavior
- Version info (`remembra --version`)

### 💡 Feature Requests

Have an idea? [Open an issue](https://github.com/remembra-ai/remembra/issues/new) with:
- Clear use case
- Proposed solution
- Alternatives considered

### 🔧 Pull Requests

1. Fork the repo
2. Create a branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Run tests (`pytest`)
5. Commit (`git commit -m 'Add amazing feature'`)
6. Push (`git push origin feature/amazing-feature`)
7. Open a Pull Request

## Development Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/remembra
cd remembra

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run linting
ruff check .
ruff format .

# Start dev server
remembra-server --reload
```

### Git hooks

Install them once per clone (they cover every worktree of it):

```bash
./scripts/install-hooks.sh
```

- `pre-commit` blocks banned words in Markdown and a wrong maintainer name in `pyproject.toml`.
- `pre-push` refuses a push that would publish private material: audit trackers under `docs/audits/`,
  `*-fixlog.md` files, and the other kinds `scripts/ci/repo_hygiene.py` lists. It checks every commit the
  push would add, not just the tip, because a file added in one commit and deleted in the next is still
  published. Run the same check by hand with `python scripts/ci/repo_hygiene.py tree` (all tracked files)
  or `python scripts/ci/repo_hygiene.py range origin/main..HEAD` (your commits); CI runs both.

Private notes (audits, fix logs, runbooks with hosts or credentials) belong outside this repository.

## Code Style

- We use [Ruff](https://github.com/astral-sh/ruff) for linting and formatting
- Type hints required for all public functions
- Docstrings for public modules, classes, and functions
- Tests for new features

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: Add conversation ingestion endpoint
fix: Handle empty query in recall
docs: Update MCP server guide
chore: Bump dependencies
```

## Questions?

- [Discord](https://discord.gg/remembra)
- [GitHub Discussions](https://github.com/remembra-ai/remembra/discussions)

Thanks for helping make Remembra better! 🚀
