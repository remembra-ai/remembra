#!/bin/bash
set -euo pipefail

# ============================================================================
# Remembra Quick Start
# One command to get AI memory running locally.
# Usage: curl -sSL https://get.remembra.dev/quickstart.sh | bash
# ============================================================================

# Colors (disabled for non-TTY)
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    RED='\033[0;31m'
    BLUE='\033[0;34m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    GREEN='' YELLOW='' RED='' BLUE='' BOLD='' NC=''
fi

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }

INSTALL_DIR="${REMEMBRA_DIR:-$HOME/.remembra}"

# Cleanup on failure
cleanup() {
    if [ $? -ne 0 ]; then
        error "Installation failed. Cleaning up..."
        if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
            docker compose -f "$INSTALL_DIR/docker-compose.yml" down 2>/dev/null || true
        fi
    fi
}
trap cleanup EXIT

echo ""
echo -e "${BOLD}🧠 Remembra — AI Memory Server${NC}"
echo "================================"
echo ""

# ---------------------------------------------------------------------------
# Check prerequisites
# ---------------------------------------------------------------------------

if ! command -v docker &> /dev/null; then
    error "Docker is required but not installed."
    echo "  Install it from: https://docker.com/get-started"
    exit 1
fi
ok "Docker found"

if ! docker compose version &> /dev/null 2>&1; then
    error "Docker Compose v2 is required."
    echo "  Update Docker Desktop or install the compose plugin."
    exit 1
fi
ok "Docker Compose v2 found"

if ! docker info &> /dev/null 2>&1; then
    error "Docker daemon is not running."
    echo "  Start Docker Desktop and try again."
    exit 1
fi
ok "Docker daemon running"

# ---------------------------------------------------------------------------
# Download compose file
# ---------------------------------------------------------------------------

info "Setting up in $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

COMPOSE_URL="https://raw.githubusercontent.com/remembra-ai/remembra/main/docker-compose.quickstart.yml"

if command -v curl &> /dev/null; then
    curl -sSL "$COMPOSE_URL" -o "$INSTALL_DIR/docker-compose.yml"
elif command -v wget &> /dev/null; then
    wget -q "$COMPOSE_URL" -O "$INSTALL_DIR/docker-compose.yml"
else
    error "curl or wget is required to download files."
    exit 1
fi
ok "Downloaded compose configuration"

# ---------------------------------------------------------------------------
# Start services
# ---------------------------------------------------------------------------

info "Starting Remembra + Qdrant + Ollama..."
docker compose -f "$INSTALL_DIR/docker-compose.yml" up -d

# Pull embedding model into Ollama
info "Downloading embedding model (nomic-embed-text)..."
docker compose -f "$INSTALL_DIR/docker-compose.yml" exec -T ollama ollama pull nomic-embed-text 2>/dev/null || {
    warn "Could not pull embedding model automatically. It will be pulled on first use."
}

# ---------------------------------------------------------------------------
# Wait for health
# ---------------------------------------------------------------------------

info "Waiting for services to be ready..."
MAX_WAIT=60
for i in $(seq 1 $MAX_WAIT); do
    if curl -sf http://localhost:8787/health > /dev/null 2>&1; then
        break
    fi
    if [ $i -eq $MAX_WAIT ]; then
        error "Timed out waiting for Remembra to start."
        echo "  Check logs with: docker compose -f $INSTALL_DIR/docker-compose.yml logs"
        exit 1
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# Success!
# ---------------------------------------------------------------------------

echo ""
echo -e "${GREEN}${BOLD}✅ Remembra is running!${NC}"
echo ""
echo -e "  ${BOLD}Server:${NC}   http://localhost:8787"
echo -e "  ${BOLD}API Docs:${NC} http://localhost:8787/docs"
echo ""
echo -e "${BOLD}Try it:${NC}"
echo ""
echo "  curl -X POST http://localhost:8787/api/v1/memories/store \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"content\": \"Alice is CEO of Acme Corp\", \"user_id\": \"demo\"}'"
echo ""
echo -e "${BOLD}Connect to Claude Desktop:${NC}"
echo ""
echo '  Add to ~/Library/Application Support/Claude/claude_desktop_config.json:'
echo ''
echo '  {'
echo '    "mcpServers": {'
echo '      "remembra": {'
echo '        "command": "remembra-mcp",'
echo '        "env": {'
echo '          "REMEMBRA_URL": "http://localhost:8787",'
echo '          "REMEMBRA_USER_ID": "default"'
echo '        }'
echo '      }'
echo '    }'
echo '  }'
echo ""
echo -e "${BOLD}Manage:${NC}"
echo "  Stop:    docker compose -f $INSTALL_DIR/docker-compose.yml down"
echo "  Logs:    docker compose -f $INSTALL_DIR/docker-compose.yml logs -f"
echo "  Restart: docker compose -f $INSTALL_DIR/docker-compose.yml restart"
echo ""
