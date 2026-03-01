# Remembra - Build Plan

**Start Date:** TBD (This Weekend?)
**MVP Target:** 12 Weeks
**Team:** Mani + General (AI)

---

## Phase 1: Foundation (Weeks 1-4)

### Week 1: Project Setup
| Task | Priority | Est. Hours |
|------|----------|------------|
| Create GitHub repo (remembra/remembra) | HIGH | 1 |
| Set up Python project structure | HIGH | 2 |
| Docker development environment | HIGH | 3 |
| CI/CD pipeline (GitHub Actions) | MEDIUM | 2 |
| Development tooling (linting, testing) | MEDIUM | 2 |

**Deliverable:** Empty project that builds and runs

### Week 2: Storage Layer
| Task | Priority | Est. Hours |
|------|----------|------------|
| Qdrant integration (vector storage) | HIGH | 6 |
| SQLite setup for metadata | HIGH | 3 |
| Memory data models | HIGH | 4 |
| Basic CRUD operations | HIGH | 4 |
| Embedding pipeline (OpenAI/local) | HIGH | 4 |

**Deliverable:** Can store and retrieve vectors

### Week 3: Python SDK
| Task | Priority | Est. Hours |
|------|----------|------------|
| Memory class implementation | HIGH | 6 |
| store() method | HIGH | 4 |
| recall() method | HIGH | 4 |
| update() method | MEDIUM | 3 |
| forget() method | MEDIUM | 2 |
| PyPI packaging | HIGH | 2 |

**Deliverable:** Working SDK on PyPI

### Week 4: Basic Intelligence
| Task | Priority | Est. Hours |
|------|----------|------------|
| LLM-powered memory extraction | HIGH | 6 |
| Semantic search implementation | HIGH | 4 |
| Basic ranking algorithm | HIGH | 4 |
| Unit tests | HIGH | 4 |
| Integration tests | MEDIUM | 3 |

**Deliverable:** Memories are intelligently extracted and recalled

---

## Phase 2: Intelligence (Weeks 5-8)

### Week 5: Entity Resolution
| Task | Priority | Est. Hours |
|------|----------|------------|
| Entity extraction from text | HIGH | 6 |
| Entity matching logic | HIGH | 6 |
| Confidence scoring | HIGH | 4 |
| Entity graph storage (SQLite) | HIGH | 4 |

**Deliverable:** System knows "Adam" = "Mr. Smith"

### Week 6: Advanced Retrieval
| Task | Priority | Est. Hours |
|------|----------|------------|
| Hybrid search (semantic + keyword) | HIGH | 6 |
| Graph-aware retrieval | HIGH | 5 |
| Context window optimization | MEDIUM | 4 |
| Relevance tuning | MEDIUM | 4 |

**Deliverable:** Recall is accurate and fast

### Week 7: REST API
| Task | Priority | Est. Hours |
|------|----------|------------|
| FastAPI implementation | HIGH | 6 |
| All endpoints (/store, /recall, etc.) | HIGH | 6 |
| Authentication (API keys) | HIGH | 4 |
| Rate limiting | MEDIUM | 2 |
| OpenAPI documentation | MEDIUM | 2 |

**Deliverable:** Full REST API

### Week 8: Temporal Features
| Task | Priority | Est. Hours |
|------|----------|------------|
| Timestamp tracking | HIGH | 3 |
| TTL (time-to-live) support | HIGH | 4 |
| Memory decay algorithm | MEDIUM | 4 |
| Historical queries (as_of) | MEDIUM | 4 |
| Performance optimization | MEDIUM | 4 |

**Deliverable:** Memories have time awareness

---

## Phase 3: Polish (Weeks 9-12)

### Week 9: Dashboard UI (Basic)
| Task | Priority | Est. Hours |
|------|----------|------------|
| React app setup | HIGH | 3 |
| Memory browser/list view | HIGH | 6 |
| Search interface | HIGH | 4 |
| Basic styling (Tailwind) | MEDIUM | 4 |

**Deliverable:** Can view memories in browser

### Week 10: Dashboard UI (Advanced)
| Task | Priority | Est. Hours |
|------|----------|------------|
| Entity graph visualization | HIGH | 6 |
| Memory editing | MEDIUM | 4 |
| Debug/explain view | MEDIUM | 4 |
| User/project management | MEDIUM | 4 |
| Analytics dashboard | LOW | 3 |

**Deliverable:** Full dashboard experience

### Week 11: Docker & Self-Host
| Task | Priority | Est. Hours |
|------|----------|------------|
| Production Dockerfile | HIGH | 4 |
| All-in-one image (API + DB + UI) | HIGH | 6 |
| Environment configuration | HIGH | 3 |
| docker-compose.yml | HIGH | 2 |
| Kubernetes Helm chart | LOW | 4 |

**Deliverable:** `docker run` works perfectly

### Week 12: Launch Prep
| Task | Priority | Est. Hours |
|------|----------|------------|
| Documentation site | HIGH | 6 |
| Landing page | HIGH | 6 |
| Example apps/tutorials | HIGH | 4 |
| Beta user outreach | HIGH | 2 |
| Bug fixes from testing | HIGH | 4 |

**Deliverable:** Ready for public beta

---

## Phase 4: Cloud & Scale (Weeks 13-16)

### Week 13-14: Cloud Infrastructure
| Task | Priority | Est. Hours |
|------|----------|------------|
| Multi-tenant architecture | HIGH | 10 |
| Usage metering | HIGH | 6 |
| Stripe billing integration | HIGH | 6 |
| Auth (Clerk/Auth0) | HIGH | 4 |

### Week 15-16: Launch
| Task | Priority | Est. Hours |
|------|----------|------------|
| Performance testing | HIGH | 6 |
| Security audit | HIGH | 6 |
| ProductHunt launch | HIGH | 4 |
| HackerNews Show HN | HIGH | 2 |
| Community building | MEDIUM | ongoing |

---

## Key Milestones

| Week | Milestone | Success Criteria |
|------|-----------|------------------|
| 4 | Working SDK | Can pip install and use |
| 8 | Full API | All features work via REST |
| 12 | MVP Launch | Docker self-host + beta users |
| 16 | Public Launch | Cloud + payments + marketing |

---

## Tech Decisions Made

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language | Python | AI/ML standard, SDK ecosystem |
| API Framework | FastAPI | Async, fast, good DX |
| Vector DB | Qdrant | Open source, easy self-host |
| Graph Storage | SQLite → Neo4j | Start simple, scale later |
| Embeddings | Model-agnostic | OpenAI, Cohere, Ollama |
| Frontend | React + Tailwind | Fast to build |
| Deployment | Docker | Universal, one-liner |

---

## Risk Checklist

- [ ] Embedding costs could get high → Support local models
- [ ] Entity resolution accuracy → Extensive testing, user feedback
- [ ] Performance at scale → Load testing in Week 15
- [ ] Competition moves fast → Ship fast, iterate faster

---

## What We're NOT Building (MVP)

- ❌ Multi-modal (images, audio) - v2
- ❌ Real-time collaboration - v2
- ❌ Mobile apps - v2
- ❌ Integrations marketplace - v2
- ❌ Advanced analytics - v2

Focus on core memory functionality first.
