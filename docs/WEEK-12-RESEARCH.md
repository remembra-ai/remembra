# Week 12: Launch Prep Research

## Documentation Site

### Tool Comparison

| Tool | Cost | Pros | Cons |
|------|------|------|------|
| **MkDocs Material** ✓ | Free | Python-native, simple, great themes | Less customizable than React |
| Docusaurus | Free | React-based, versioning | Overkill for our size |
| Mintlify | $300/mo | Beautiful, AI-ready | Too expensive for MVP |
| GitBook | Freemium | Easy WYSIWYG | Limited free tier |

**Decision: MkDocs Material**
- Python project = Python docs tool
- Beautiful themes (Material)
- Simple deployment (GitHub Pages or static serve)
- Zero cost

### Documentation Structure

```
docs/
├── index.md                 # Quick intro
├── getting-started/
│   ├── quickstart.md        # 5-minute start
│   ├── installation.md      # All install methods
│   └── docker.md            # Docker deployment
├── guides/
│   ├── python-sdk.md        # SDK reference
│   ├── rest-api.md          # API reference
│   ├── entity-resolution.md # Feature deep-dive
│   ├── temporal.md          # TTL, decay, as_of
│   └── security.md          # Auth, rate limits
├── concepts/
│   ├── how-it-works.md      # Architecture overview
│   ├── memory-types.md      # Facts vs relationships
│   └── entity-graph.md      # Entity system
├── examples/
│   ├── chatbot.md           # Basic chatbot memory
│   ├── rag-pipeline.md      # RAG integration
│   └── multi-user.md        # SaaS use case
└── reference/
    ├── configuration.md     # All env vars
    ├── api.md               # OpenAPI spec
    └── changelog.md         # Version history
```

---

## Landing Page

### Best Practices (2026)

1. **Hero Section**
   - Clear headline: "AI Memory That Actually Works"
   - Subheadline: "Self-host in 5 minutes. Remember everything."
   - CTA: "Get Started" / "View on GitHub"
   - Code snippet preview

2. **Problem/Solution**
   - "Every AI forgets. Yours doesn't have to."
   - Visual showing memory persistence

3. **Features Grid**
   - Smart Extraction
   - Entity Resolution
   - Temporal Memory
   - Hybrid Search
   - Graph Visualization
   - Self-Host First

4. **Code Example**
   - Live/animated code snippet
   - Show store → recall flow

5. **Comparison Table**
   - Remembra vs Mem0 vs Zep vs DIY
   - Highlight: pricing, self-host, features

6. **Social Proof**
   - GitHub stars
   - "Used by" logos (when we have them)
   - Testimonials (later)

7. **Pricing (Simple)**
   - Open Source: Free forever
   - Cloud (Coming Soon): Fair tiers

8. **Footer CTA**
   - "Get Started" button
   - GitHub / Discord / Docs links

### Tech Stack for Landing Page

| Option | Pros | Cons |
|--------|------|------|
| **Astro** ✓ | Fast, static, modern | Learning curve |
| Next.js | Full-featured | Overkill for static |
| Plain HTML | Simple | No components |

**Decision: Astro + Tailwind**
- Fast static site
- Component-based
- Easy to maintain
- Great DX

---

## Week 12 Task Breakdown

### Day 1-2: Documentation Site
- [ ] Set up MkDocs Material
- [ ] Write quickstart.md
- [ ] Write installation.md  
- [ ] Write docker.md
- [ ] Write python-sdk.md

### Day 3-4: More Docs
- [ ] Write rest-api.md
- [ ] Write entity-resolution.md
- [ ] Write temporal.md
- [ ] Write security.md
- [ ] Write configuration.md

### Day 5-6: Landing Page
- [ ] Set up Astro project
- [ ] Build hero section
- [ ] Build features grid
- [ ] Build code example
- [ ] Build comparison table
- [ ] Build footer

### Day 7: Polish & Deploy
- [ ] Deploy docs (GitHub Pages or Vercel)
- [ ] Deploy landing page
- [ ] Link everything together
- [ ] Test all links
- [ ] Prepare beta outreach list

---

## Beta Outreach Plan

### Targets
1. **AI Discord communities** - Claude, ChatGPT builders
2. **Reddit** - r/LocalLLaMA, r/MachineLearning
3. **Twitter/X** - AI devs, indie hackers
4. **Hacker News** - Show HN post
5. **Direct outreach** - Clawdbot users, past contacts

### Messaging
> "We built Remembra because every AI forgets everything. 
> Self-host in 5 min with Docker. Open source. 
> Curious what you think: [link]"

Keep it casual, ask for feedback, not sales.
