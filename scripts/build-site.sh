#!/bin/bash
# Build Remembra docs and landing page

set -e

echo "🔨 Building documentation..."
cd /Users/dolphy/projects/remembra
uv run mkdocs build

echo "📄 Copying landing page..."
cp landing/index.html site/landing.html

echo "✅ Build complete!"
echo "   - Docs: site/"
echo "   - Landing: site/landing.html"
echo ""
echo "To serve locally:"
echo "   cd site && python -m http.server 8000"
