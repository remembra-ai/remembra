# AI Memory Layer - Product Specification

**Codename:** Remembra (working title)
**Version:** 1.0 Draft
**Date:** 2026-02-28
**Author:** DolphyTech

---

## 🎯 Executive Summary

**What We're Building:**
A universal memory layer for AI applications. One SDK that gives any LLM persistent, intelligent memory across sessions.

**The Problem:**
Every AI forgets everything. Developers building AI apps have to hack together memory solutions using vector databases, embeddings, and custom retrieval logic. It's complex, fragmented, and everyone's rebuilding the same thing.

**Our Solution:**
Simple memory primitives that just work:
```python
from remembra import Memory

memory = Memory(user_id="user_123")
memory.store("User prefers dark mode and hates long emails")
context = memory.recall("What are user's preferences?")
```

**Why We Win:**
- **Self-host in minutes** (competitors prioritize SaaS, neglect self-hosting)
- **Fair pricing** ($0 → $29 → $99, not $19 → $249 like Mem0)
- **Open source core** (MIT license, not AGPL restrictions)
- **Hybrid storage** (vector + graph + relational in one system)
- **Entity resolution** (knows "Adam" = "Adam Smith" = "Mr. Smith")

---

## 🏆 Competitive Analysis

### Why Existing Solutions Fall Short

| Competitor | Weakness We Exploit |
|------------|---------------------|
| **Mem0** | Self-hosting docs are trash, $19→$249 price jump, closed source |
| **Zep** | Academic/complex, requires significant effort to deploy |
| **Letta** | Not production-ready, slow and token-expensive |
| **LangChain Memory** | Too basic, hard to debug, no persistence |

### Our Differentiators

| Feature | Mem0 | Zep | Letta | **Remembra** |
|---------|------|-----|-------|--------------|
| Self-host ease | ❌ Poor | ⚠️ Complex | ✅ OK | ✅ **5 min Docker** |
| Open source | ❌ No | ⚠️ Partial | ✅ Yes | ✅ **MIT License** |
| Pricing | $19-249 | $19-475 | Free/12 | **$0-29-99** |
| Entity resolution | ⚠️ Basic | ✅ Good | ❌ No | ✅ **AI-native** |
| Hybrid storage | ❌ Vector only | ✅ Graph | ❌ LLM only | ✅ **All three** |
| Debug/Observability | ❌ Black box | ⚠️ Limited | ⚠️ Limited | ✅ **Full dashboard** |

---

## 🔧 Core Capabilities

### 1. Memory Operations (MVP)

```python
# Initialize
from remembra import Memory
memory = Memory(user_id="user_123", project="my_app")

# Store - automatic extraction and categorization
memory.store("Had a great call with John. He's the CTO at Acme Corp. Prefers email over Slack.")

# Recall - semantic search with context
context = memory.recall("Who is John?")
# Returns: "John is the CTO at Acme Corp. Prefers email communication."

# Update - intelligent merging
memory.update("John got promoted to CEO")
# Automatically updates John's role, keeps other facts

# Forget - GDPR compliant deletion
memory.forget(entity="John")  # Removes all memories about John
```

### 2. Entity Resolution (Differentiator)

The hardest problem in memory systems: knowing that "Adam", "Adam Smith", "Mr. Smith", and "my husband" are the same person.

```python
# Automatic entity linking
memory.store("Adam called me yesterday")
memory.store("Mr. Smith wants to reschedule")
memory.store("My husband Adam is running late")

# System knows these are all the same person
context = memory.recall("What about Adam?")
# Returns unified context about Adam/Mr. Smith/husband
```

**How it works:**
- AI-powered entity extraction on store
- Confidence scoring for entity matches
- User can confirm/reject entity links via dashboard
- Graph database tracks relationships

### 3. Temporal Awareness

Memories have time context:

```python
# Time-aware storage
memory.store("User is on vacation", ttl="2 weeks")

# Time-aware recall
context = memory.recall("What's user doing?", as_of="last month")

# Automatic decay
# Old, unreferenced memories get lower priority
# Recent, frequently-accessed memories stay prominent
```

### 4. Multi-Modal Memory (Phase 2)

```python
# Store structured data
memory.store({"meeting": "standup", "time": "9am", "recurring": True})

# Store from conversation
memory.store_conversation(messages=[...])

# Store from documents
memory.store_document("path/to/file.pdf")
```

### 5. Observability Dashboard

Web UI for:
- Viewing all stored memories
- Entity relationship graph visualization
- Memory access logs
- Debug why specific recalls returned what they did
- Manual memory editing
- Usage analytics

---

## 🏗️ Technical Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                         API Layer                           │
│              REST API  /  Python SDK  /  JS SDK             │
├─────────────────────────────────────────────────────────────┤
│                    Intelligence Engine                       │
│  ┌─────────────┐  ┌─────────────┐  ┌───────────────────┐   │
│  │ Extraction  │→ │ Entity Res. │→ │ Ranking/Retrieval │   │
│  │   (LLM)     │  │  (LLM+Graph)│  │   (Hybrid)        │   │
│  └─────────────┘  └─────────────┘  └───────────────────┘   │
├─────────────────────────────────────────────────────────────┤
│                    Storage Layer (Hybrid)                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Vector Store │  │ Graph Store  │  │ Relational Store │  │
│  │   (Qdrant)   │  │  (Neo4j/     │  │   (Postgres)     │  │
│  │              │  │   SQLite)    │  │                  │  │
│  │ Semantic     │  │ Entities &   │  │ Metadata,        │  │
│  │ Search       │  │ Relations    │  │ Users, Projects  │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│                    Embedding Layer                           │
│         OpenAI / Cohere / Ollama (local) / Custom           │
└─────────────────────────────────────────────────────────────┘
```

### Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| **Core Language** | Python | SDK ecosystem, AI/ML standard |
| **API Framework** | FastAPI | Async, fast, good docs |
| **Vector DB** | Qdrant | Open source, fast, easy self-host |
| **Graph DB** | SQLite + custom (MVP) → Neo4j (scale) | Start simple, scale later |
| **Relational DB** | PostgreSQL / SQLite | Standard, reliable |
| **Embeddings** | Model-agnostic | OpenAI, Cohere, local Ollama |
| **LLM for extraction** | Model-agnostic | Any OpenAI-compatible API |
| **Dashboard** | React + Tailwind | Fast to build, looks good |
| **Deployment** | Docker | One-liner self-host |

### Self-Hosting (Our Moat)

**The One-Liner:**
```bash
docker run -d -p 8787:8787 remembra/remembra
```

**What's included:**
- All storage engines bundled
- Dashboard UI
- API server
- Default local embeddings (no API key needed)
- SQLite for zero-config start
- Upgrade path to Postgres/Qdrant/Neo4j

**Why this matters:**
- Mem0/Zep require multiple services, config files, external DBs
- We bundle everything, works out of the box
- Developers can try it in 30 seconds

---

## 💰 Pricing Strategy

### Tiers

| Tier | Price | Limits | Target |
|------|-------|--------|--------|
| **Free** | $0 | 50K memories, 1 project, community support | Indie devs, students |
| **Pro** | $29/mo | 500K memories, 5 projects, email support | Startups, side projects |
| **Team** | $99/mo | 2M memories, unlimited projects, priority support | Growing companies |
| **Enterprise** | Custom | Unlimited, SLA, dedicated support, on-prem | Large orgs |

### Why This Beats Competition

| | Mem0 | Zep | **Remembra** |
|--|------|-----|--------------|
| Free tier | 10K memories | 1K episodes | **50K memories** |
| First paid | $19/mo | $19/mo | **$29/mo** |
| Mid-tier | $249/mo 🤮 | $249/mo | **$99/mo** |
| Self-host | Limited | Complex | **Always free** |

**Key insight:** The $19 → $249 jump is where Mem0/Zep lose customers. We capture that mid-market with $99.

### Revenue Model

1. **Cloud SaaS** - Usage-based after tier limits
2. **Self-host support** - Enterprise support contracts
3. **Managed on-prem** - Deploy in customer's cloud, we manage

---

## 📅 Build Plan (12 Weeks to MVP)

### Phase 1: Core Foundation (Weeks 1-4)

**Week 1: Project Setup**
- [ ] Repo setup, CI/CD pipeline
- [ ] FastAPI boilerplate
- [ ] Docker development environment
- [ ] Basic project structure

**Week 2: Storage Layer**
- [ ] Qdrant integration (vector storage)
- [ ] SQLite for metadata/relational
- [ ] Memory CRUD operations
- [ ] Basic embedding pipeline

**Week 3: Python SDK**
- [ ] `Memory` class implementation
- [ ] `store()`, `recall()`, `update()`, `forget()`
- [ ] User/project management
- [ ] SDK packaging (PyPI)

**Week 4: Basic Intelligence**
- [ ] LLM-powered memory extraction
- [ ] Semantic search retrieval
- [ ] Basic ranking algorithm
- [ ] Testing suite

**Milestone:** Working SDK, can store and recall memories

---

### Phase 2: Intelligence Layer (Weeks 5-8)

**Week 5: Entity Resolution**
- [ ] Entity extraction from memories
- [ ] Entity matching/linking logic
- [ ] Confidence scoring
- [ ] Entity graph storage

**Week 6: Advanced Retrieval**
- [ ] Hybrid search (semantic + keyword + graph)
- [ ] Context window optimization
- [ ] Relevance ranking improvements
- [ ] Query understanding

**Week 7: REST API**
- [ ] Full API implementation
- [ ] Authentication (API keys)
- [ ] Rate limiting
- [ ] API documentation (OpenAPI)

**Week 8: Temporal Features**
- [ ] Time-based memory storage
- [ ] TTL support
- [ ] Memory decay algorithm
- [ ] Historical queries

**Milestone:** Production-quality API with intelligent retrieval

---

### Phase 3: Dashboard & Deployment (Weeks 9-12)

**Week 9: Dashboard UI**
- [ ] React app setup
- [ ] Memory browser/viewer
- [ ] Search interface
- [ ] Basic analytics

**Week 10: Dashboard Advanced**
- [ ] Entity graph visualization
- [ ] Memory editing
- [ ] Debug/explain view
- [ ] User management

**Week 11: Docker & Self-Host**
- [ ] Production Docker image
- [ ] All-in-one bundle
- [ ] Environment configuration
- [ ] Upgrade/migration scripts

**Week 12: Launch Prep**
- [ ] Documentation site
- [ ] Landing page
- [ ] Example apps/tutorials
- [ ] Beta user onboarding

**Milestone:** Shippable MVP, ready for beta users

---

### Phase 4: Cloud & Scale (Weeks 13-16)

**Week 13-14: Cloud Infrastructure**
- [ ] Multi-tenant architecture
- [ ] Usage metering/billing
- [ ] Stripe integration
- [ ] Auth0/Clerk integration

**Week 15-16: Scale & Polish**
- [ ] Performance optimization
- [ ] Monitoring/alerting
- [ ] Security audit
- [ ] Public launch

---

## 🚀 Go-To-Market Strategy

### Launch Sequence

1. **Week 10:** Private alpha (10-20 developers we know)
2. **Week 12:** Public beta announcement
3. **Week 14:** ProductHunt launch
4. **Week 16:** HackerNews Show HN

### Positioning

**Tagline options:**
- "Memory for AI. Finally."
- "Give your AI a brain."
- "Persistent memory for LLMs, self-hosted in minutes."

**Key messages:**
1. Self-host in minutes (vs. complex Mem0/Zep setup)
2. Fair pricing that doesn't jump 10x
3. Open source, own your data
4. Built by developers, for developers

### Channels

1. **Dev communities:** Reddit (r/LocalLLaMA, r/MachineLearning), HackerNews, Dev.to
2. **Social:** Twitter/X AI community, LinkedIn
3. **Content:** Blog posts, tutorials, YouTube demos
4. **Open source:** GitHub stars, community contributions

---

## 📊 Success Metrics

### MVP (Week 12)
- [ ] 100 GitHub stars
- [ ] 50 beta signups
- [ ] 10 active users
- [ ] SDK works reliably

### Month 3
- [ ] 500 GitHub stars
- [ ] 200 registered users
- [ ] 20 paying customers
- [ ] $500 MRR

### Month 6
- [ ] 2,000 GitHub stars
- [ ] 1,000 registered users
- [ ] 100 paying customers
- [ ] $5,000 MRR

### Year 1
- [ ] 10,000 GitHub stars
- [ ] 5,000 registered users
- [ ] 500 paying customers
- [ ] $15,000-40,000 MRR ($150K-500K ARR)

---

## 🔐 Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Mem0 copies our features | High | Medium | Move fast, build community, open source moat |
| Technical complexity | Medium | High | Start simple, iterate. SQLite before Neo4j |
| Pricing pressure | Medium | Medium | Volume over margin, enterprise upsells |
| LLM costs | Medium | Medium | Support local models, optimize prompts |
| Low adoption | Medium | High | Strong content marketing, free tier |

---

## 💡 Future Roadmap (Post-MVP)

### Version 2.0 Features
- Multi-modal memory (images, audio, video)
- Memory sharing across apps
- Collaborative memories (team knowledge bases)
- Automatic memory consolidation
- Integration marketplace (Notion, Slack, etc.)

### Potential Pivots
- If B2D fails → Enterprise sales focus
- If self-host dominates → Support/consulting revenue
- If cloud wins → Usage-based pricing

---

## ✅ Immediate Next Steps

1. **Tonight:** Finalize this spec, pick a name
2. **This weekend:** Set up repo, Docker dev environment
3. **Week 1:** Core storage layer, basic SDK
4. **Week 2:** First working prototype

---

*This is a living document. Update as we learn.*
