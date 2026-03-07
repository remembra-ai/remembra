# Competitor Analysis - AI Memory Layer Market (March 2026)

## Market Players

### 1. Mem0 (mem0.ai)
**Positioning:** Universal memory layer for AI Agents
**Funding:** YC-backed, 50K+ developers

**Pricing:**
| Tier | Price | Memories | Retrievals/mo |
|------|-------|----------|---------------|
| Hobby | Free | 10K | 1K |
| Starter | $19/mo | 50K | 5K |
| Pro | $249/mo | Unlimited | 50K |
| Enterprise | Custom | - | - |

**Key Features:**
- ✅ Memory compression (80% token reduction claim)
- ✅ Graph memory (Pro only - $249/mo)
- ✅ Self-hosting option (OSS)
- ✅ Python & JS SDKs
- ✅ MCP server
- ✅ SOC 2, HIPAA, BYOK

**Weaknesses:**
- ❌ Steep pricing jump ($19 → $249 for graph)
- ❌ Graph memory paywalled
- ❌ Can feel heavy for simple use cases

---

### 2. Zep (getzep.com)
**Positioning:** Context engineering platform
**Focus:** Temporal knowledge graphs, entity extraction

**Pricing:**
| Tier | Price | Credits/mo |
|------|-------|------------|
| Free | $0 | 1K (rate-limited) |
| Flex | $25/mo | 20K (auto-topup) |
| Enterprise | Custom | - |

**Key Features:**
- ✅ Temporal knowledge graph (killer feature)
- ✅ Context assembly for LLMs
- ✅ Business data ingestion (JSON)
- ✅ Custom entity types
- ✅ Python, TS, Go SDKs
- ✅ Graphiti OSS (graph layer)
- ✅ SOC 2 Type II, HIPAA BAA

**Weaknesses:**
- ❌ Complexity (learning curve)
- ❌ Credit-based pricing (confusing)
- ❌ Cloud only (no self-host)
- ❌ Weak free tier

---

### 3. LangMem (LangChain)
**Positioning:** Library for LangGraph agents
**Model:** Open source (MIT)

**Pricing:** Free (OSS)

**Key Features:**
- ✅ Free and open source
- ✅ Deep LangGraph integration
- ✅ Background memory manager
- ✅ Prompt refinement
- ✅ Flexible storage backends

**Weaknesses:**
- ❌ LangGraph-only
- ❌ Python only
- ❌ No managed option
- ❌ You manage everything
- ❌ No knowledge graph
- ❌ Thin documentation

---

### 4. MemoClaw
**Positioning:** Simple memory-as-a-service
**Model:** Pay-per-use with crypto

**Pricing:** 
- Free: 1K API calls
- $0.001/store, $0.001/recall (pay as you go)

**Key Features:**
- ✅ Dead simple API (store/recall)
- ✅ No API keys (wallet auth)
- ✅ Pay-per-use (no subscriptions)
- ✅ MCP server
- ✅ TypeScript & Python SDKs

**Weaknesses:**
- ❌ No knowledge graph
- ❌ Cloud only
- ❌ Crypto wallet required
- ❌ OpenAI embeddings only
- ❌ 8K char limit per memory
- ❌ No compliance certs

---

## Remembra Positioning

### Our Unique Value Props

| Feature | Mem0 | Zep | LangMem | MemoClaw | **Remembra** |
|---------|------|-----|---------|----------|-------------|
| Self-host in 5 min | Complex | ❌ | Complex | ❌ | ✅ Docker one-liner |
| Knowledge graph | $249/mo | ✅ | ❌ | ❌ | ✅ Free |
| Entity resolution | ✅ | ✅ | ❌ | ❌ | ✅ Free |
| Temporal (TTL/decay) | ❌ | ✅ | ❌ | ❌ | ✅ |
| MCP server | ✅ | ✅ | ❌ | ✅ | ✅ |
| Webhooks | ❌ | ❌ | ❌ | ❌ | ✅ |
| RBAC | Enterprise | ❌ | ❌ | ❌ | ✅ Free |
| Import ChatGPT/Claude | ❌ | ❌ | ❌ | ❌ | ✅ |
| Plugin system | ❌ | ❌ | ❌ | ❌ | ✅ |
| Hybrid search | ✅ | ✅ | ✅ | ✅ | ✅ BM25+vector+graph |
| Open source | ✅ | Partial | ✅ | ❌ | ✅ MIT |
| Python SDK | ✅ | ✅ | ✅ | ✅ | ✅ |
| JS/TS SDK | ✅ | ✅ | ❌ | ✅ | ✅ |

### Where We Win

1. **Best free tier** - Graph + entities + temporal + RBAC all free (Mem0 charges $249)
2. **Simplest self-hosting** - One Docker command vs complex setup
3. **MCP-first** - Built for Claude Code / Cursor era
4. **Enterprise features free** - Webhooks, RBAC, audit logs in open source
5. **Migration path** - Import from ChatGPT, Claude, other providers
6. **Extensible** - Plugin system for custom logic

### Pricing Strategy

**Recommended Pricing (if offering cloud):**

| Tier | Price | Memories | API Calls/mo | Features |
|------|-------|----------|--------------|----------|
| Self-Hosted | Free | Unlimited | Unlimited | All features |
| Cloud Starter | $9/mo | 25K | 10K | All features |
| Cloud Pro | $49/mo | 100K | 50K | Priority support |
| Cloud Enterprise | Custom | Unlimited | Unlimited | SLA, SSO, audit |

**Key differentiator:** No paywall on core features (graph, entities, temporal). Charge for scale and support.

---

## User Journey Considerations

### Current Gap: Onboarding Flow

**Question:** When someone subscribes, how do they get set up?

**Required Flow:**
1. **Discovery** - Land on remembra.dev
2. **Understand** - See value props, compare to competitors
3. **Try** - Quick start (Docker or cloud signup)
4. **Integrate** - SDK/MCP/REST API
5. **Scale** - Upgrade plan, add team members
6. **Monitor** - Dashboard, usage metrics

**Missing Pieces:**
- [ ] Cloud signup flow (Stripe checkout)
- [ ] Dashboard for cloud users
- [ ] Team/org management
- [ ] Usage billing
- [ ] Onboarding wizard

---

## Landing Page Requirements

### Must Have
1. Hero with clear value prop
2. Feature comparison table (vs competitors)
3. "Self-host in minutes" demo
4. MCP integration highlight
5. Pricing section (clear, competitive)
6. Quick start code snippets
7. Use cases with examples
8. CTA buttons (Get Started, View Docs, GitHub)

### Should Have
1. Testimonials / social proof
2. Case studies
3. Interactive demo
4. GitHub stars badge
5. Discord community link
6. Blog / changelog

### Nice to Have
1. Video walkthrough
2. ROI calculator
3. Migration guides from competitors
4. API playground
