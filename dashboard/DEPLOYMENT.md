# Remembra Dashboard Deployment Guide

## Overview

The Remembra Dashboard is a React SPA that needs to be deployed to `app.remembra.dev`.

**Current Status:**
- ✅ Dashboard built and ready (`dashboard/dist/`)
- ⏳ Backend API needs deployment
- ⏳ DNS records need configuration
- ⏳ Coolify deployment needed

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Coolify (178.156.226.84)                  │
├─────────────────────────────────────────────────────────────────┤
│  remembra.dev        → Landing page (static HTML)               │
│  app.remembra.dev    → Dashboard (React SPA) ← THIS DEPLOYMENT  │
│  api.remembra.dev    → Backend API (Python/FastAPI)             │
└─────────────────────────────────────────────────────────────────┘
```

## Prerequisites

### 1. DNS Records (Cloudflare/DNS Provider)

Add these A records:
```
app.remembra.dev  →  178.156.226.84
api.remembra.dev  →  178.156.226.84
```

### 2. Backend API Deployment

The dashboard needs the API to be running at `api.remembra.dev`.

Deploy the backend first:
1. In Coolify, create new service from `/Users/dolphy/projects/remembra`
2. Use `Dockerfile.cloud` 
3. Domain: `api.remembra.dev`
4. Set environment variables:
   - `REMEMBRA_OPENAI_API_KEY`
   - `REMEMBRA_QDRANT_URL` (Qdrant Cloud or local)
   - `REMEMBRA_AUTH_ENABLED=true`
   - `REMEMBRA_AUTH_MASTER_KEY` (for generating API keys)
   - `REMEMBRA_STRIPE_SECRET_KEY`
   - `REMEMBRA_STRIPE_WEBHOOK_SECRET`

## Dashboard Deployment

### Option 1: Docker (Recommended for Coolify)

**Build locally:**
```bash
cd dashboard
docker build -t remembra-dashboard \
  --build-arg VITE_API_URL=https://api.remembra.dev \
  .
```

**Deploy to Coolify:**
1. Create new resource → Docker Compose or Dockerfile
2. Point to `dashboard/Dockerfile`
3. Set domain: `app.remembra.dev`
4. Build args: `VITE_API_URL=https://api.remembra.dev`
5. Deploy

### Option 2: Static Files (Simpler)

**Build:**
```bash
cd dashboard
VITE_API_URL=https://api.remembra.dev npm run build
```

**Upload:**
1. Copy `dist/` contents to server
2. Serve via nginx/caddy

## CORS Configuration

The backend API must allow requests from `app.remembra.dev`.

Add to backend `.env`:
```
REMEMBRA_CORS_ORIGINS=https://app.remembra.dev,https://remembra.dev
```

Or in the code (`src/remembra/api/app.py`):
```python
ALLOWED_ORIGINS = [
    "https://app.remembra.dev",
    "https://remembra.dev",
    "http://localhost:5173",  # dev
]
```

## Verification

After deployment:

```bash
# Check dashboard is accessible
curl -I https://app.remembra.dev

# Check API is accessible
curl https://api.remembra.dev/health

# Check CORS headers
curl -H "Origin: https://app.remembra.dev" \
     -I https://api.remembra.dev/api/v1/health
```

## Environment Variables

| Variable | Description | Where |
|----------|-------------|-------|
| `VITE_API_URL` | API base URL | Dashboard build |
| `REMEMBRA_CORS_ORIGINS` | Allowed origins | Backend |

## Troubleshooting

### "Network Error" in Dashboard
- Check API is running: `curl https://api.remembra.dev/health`
- Check CORS: Browser console will show CORS errors
- Verify `VITE_API_URL` was set at build time

### 502 Bad Gateway
- Container not running in Coolify
- Check Coolify logs

### SSL/Certificate Issues
- Coolify handles SSL via Let's Encrypt
- Ensure domain DNS is properly configured

---

## Quick Deploy Checklist

- [ ] DNS: `app.remembra.dev` → `178.156.226.84`
- [ ] DNS: `api.remembra.dev` → `178.156.226.84`
- [ ] Backend deployed to `api.remembra.dev`
- [ ] Backend CORS allows `https://app.remembra.dev`
- [ ] Dashboard built with `VITE_API_URL=https://api.remembra.dev`
- [ ] Dashboard deployed to `app.remembra.dev`
- [ ] Test: Dashboard loads at `https://app.remembra.dev`
- [ ] Test: Login works (API key accepted)
