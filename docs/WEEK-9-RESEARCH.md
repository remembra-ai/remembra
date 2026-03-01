# Week 9 Research: Dashboard UI

**Date:** 2026-03-01
**Status:** APPROVED (using best practices)

---

## Framework Decision

**Choice: Vite + React + Tailwind + shadcn/ui**

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Vite + React** | Fast, modern, simple | Need to add UI lib | ✅ Best for our size |
| Next.js | SSR, full framework | Overkill for dashboard | ❌ Too heavy |
| React-Admin | Built-in CRUD | Opinionated, learning curve | ❌ Not flexible enough |
| CoreUI | Complete admin kit | Heavy, paid features | ❌ |

**UI Components: shadcn/ui**
- Copy-paste components (not a dependency)
- Tailwind-based (matches our stack)
- Beautiful defaults
- Full customization
- MIT license

---

## Core Views

### 1. Memory Browser (Main View)
```
┌─────────────────────────────────────────────────────────┐
│ 🧠 Remembra Dashboard                    [user@project] │
├─────────────────────────────────────────────────────────┤
│ 🔍 Search memories...                      [Filters ▾]  │
├─────────────────────────────────────────────────────────┤
│ ┌─────────────────────────────────────────────────────┐ │
│ │ John is the CTO at Acme Corp              [0.95] ⋮ │ │
│ │ 📅 Mar 1, 2026  👤 John, Acme        🏷️ semantic   │ │
│ └─────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ User prefers dark mode                    [0.87] ⋮ │ │
│ │ 📅 Feb 28, 2026  👤 User            🏷️ preference │ │
│ └─────────────────────────────────────────────────────┘ │
│                                                         │
│ [Load More]                            Showing 10 of 47 │
└─────────────────────────────────────────────────────────┘
```

### 2. Search Interface
- Real-time semantic search (debounced)
- Filter by: date range, entity, memory type
- Sort by: relevance, date, access count

### 3. Memory Detail
- Full content view
- Linked entities (clickable)
- Metadata (created, accessed, TTL)
- Edit/Delete actions
- Related memories

---

## API Integration

Dashboard connects to existing REST API:
```javascript
// Fetch memories
GET /api/v1/memories?limit=20&offset=0

// Search
POST /api/v1/memories/recall
{ "query": "...", "limit": 10 }

// Memory detail
GET /api/v1/memories/{id}

// Delete
DELETE /api/v1/memories/{id}
```

---

## Project Structure

```
dashboard/
├── index.html
├── package.json
├── vite.config.ts
├── tailwind.config.js
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── components/
│   │   ├── ui/           # shadcn components
│   │   ├── MemoryCard.tsx
│   │   ├── MemoryList.tsx
│   │   ├── SearchBar.tsx
│   │   └── Sidebar.tsx
│   ├── pages/
│   │   ├── Dashboard.tsx
│   │   ├── MemoryDetail.tsx
│   │   └── Settings.tsx
│   ├── hooks/
│   │   ├── useMemories.ts
│   │   └── useSearch.ts
│   └── lib/
│       └── api.ts
└── dist/              # Built output
```

---

## Build Tasks

| Task | Est. Hours |
|------|------------|
| Vite + React + Tailwind setup | 1 |
| shadcn/ui components (card, input, button) | 1 |
| API client (fetch wrapper) | 1 |
| Memory list view | 2 |
| Search with debounce | 1 |
| Memory card component | 1 |
| Filters (date, type) | 2 |
| Memory detail page | 2 |
| Dark mode toggle | 0.5 |
| Build + serve config | 0.5 |

**Total: ~12 hours**

---

## Serving Options

1. **Standalone** (default): `npm run dev` on port 5173
2. **Embedded**: FastAPI serves `/dashboard` from built files
3. **Docker**: Include in Remembra image

Recommendation: Build standalone first, embed in Week 10.

---

*Research complete. Ready to build.*
