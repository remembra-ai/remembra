# Remembra Deployment Guide

Auto-start Remembra on boot so it's always running.

---

## Quick Start (Docker)

**Recommended for production:**

```bash
docker run -d \
  --name remembra \
  --restart=always \
  -p 8787:8787 \
  -v remembra_data:/data \
  -e REMEMBRA_AUTH_ENABLED=true \
  -e REMEMBRA_AUTH_MASTER_KEY=your-secret-key \
  remembra/remembra
```

That's it. Survives reboots, auto-restarts on crash.

---

## macOS (launchd)

### 1. Create the service file

```bash
cat > ~/Library/LaunchAgents/com.remembra.server.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.remembra.server</string>
    
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/uv</string>
        <string>run</string>
        <string>remembra</string>
    </array>
    
    <key>WorkingDirectory</key>
    <string>/path/to/remembra</string>
    
    <key>RunAtLoad</key>
    <true/>
    
    <key>KeepAlive</key>
    <true/>
    
    <key>StandardOutPath</key>
    <string>/tmp/remembra.log</string>
    
    <key>StandardErrorPath</key>
    <string>/tmp/remembra.error.log</string>
</dict>
</plist>
EOF
```

### 2. Update paths

Edit the plist and replace:
- `/usr/local/bin/uv` → your uv path (`which uv`)
- `/path/to/remembra` → your Remembra directory

### 3. Load the service

```bash
launchctl load ~/Library/LaunchAgents/com.remembra.server.plist
```

### 4. Verify

```bash
curl http://localhost:8787/health
```

### Management commands

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.remembra.server.plist

# Start
launchctl load ~/Library/LaunchAgents/com.remembra.server.plist

# View logs
tail -f /tmp/remembra.log
```

---

## Linux (systemd)

### 1. Create the service file

```bash
sudo cat > /etc/systemd/system/remembra.service << 'EOF'
[Unit]
Description=Remembra Memory Server
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/remembra
ExecStart=/usr/local/bin/uv run remembra
Restart=always
RestartSec=10
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
EOF
```

### 2. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable remembra
sudo systemctl start remembra
```

### 3. Verify

```bash
sudo systemctl status remembra
curl http://localhost:8787/health
```

### Management commands

```bash
sudo systemctl stop remembra
sudo systemctl start remembra
sudo systemctl restart remembra
journalctl -u remembra -f  # View logs
```

---

## Qdrant (Required)

Remembra needs Qdrant running. Same auto-start setup:

### Docker (easiest)

```bash
docker run -d \
  --name qdrant \
  --restart=always \
  -p 6333:6333 \
  -v qdrant_data:/qdrant/storage \
  qdrant/qdrant
```

### macOS (Homebrew)

```bash
brew services start qdrant
```

---

## Environment Variables

Create `.env` in your Remembra directory:

```bash
# Required
REMEMBRA_QDRANT_URL=http://localhost:6333
REMEMBRA_OPENAI_API_KEY=sk-xxx

# Auth (enable for production)
REMEMBRA_AUTH_ENABLED=true
REMEMBRA_AUTH_MASTER_KEY=your-secret-master-key

# Optional
REMEMBRA_PORT=8787
REMEMBRA_LOG_LEVEL=info
```

---

## Health Check

After setup, verify everything works:

```bash
# Server health
curl http://localhost:8787/health

# Expected response:
# {"status":"ok","version":"0.8.0","dependencies":{"qdrant":{"status":"ok",...}}}
```

---

## Troubleshooting

### Server not starting

1. Check logs: `tail -f /tmp/remembra.log`
2. Verify Qdrant: `curl http://localhost:6333/health`
3. Check port: `lsof -i :8787`

### Qdrant connection failed

1. Start Qdrant: `docker start qdrant`
2. Check URL in `.env` matches Qdrant location

### API key errors

1. Create a key: `curl -X POST http://localhost:8787/api/v1/keys -H "X-API-Key: $MASTER_KEY"`
2. Use the returned key in your client

---

*Remembra should survive reboots and auto-restart on crash. Set it and forget it.*
