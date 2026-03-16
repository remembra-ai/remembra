# BUG-002: Space Namespace Mismatch

**Type:** Product/Data-Model Bug  
**Severity:** Critical  
**Discovered:** 2026-03-16  
**Status:** Needs Fix  
**Priority:** P0 — Near-term, not backlog

---

## Prioritization (per Mani)

**Approach:** Option A first (auto-detection). Defer architecture merge questions until UX and discovery path are stable.

**Reasoning:**
- Option A fixes user-facing failure quickly without locking into model decision too early
- Option B is a product simplification decision, not an incident fix
- Option C alone improves visibility but doesn't prevent the bug

**Implementation Order:**
1. Backend namespace discovery endpoint
2. Dashboard create-space flow (dropdown defaulting to active/populated namespace)
3. Projects mismatch banner + relink workflow
4. Tests for namespace-aware creation and empty-state prevention  

---

## Summary

New spaces default to `project_id = 'default'`, but existing memories may live in `clawdbot` or another namespace. This causes the dashboard to appear empty even when the user has hundreds of memories.

---

## Core Problem

- `project_id` is the authoritative memory namespace
- Every space must explicitly map to one `project_id`
- The dashboard must **never assume `default`** unless it has verified that is the user's active namespace

---

## Required Changes

### 1. Create-Space Flow

- [ ] Default `project_id` to the **active namespace**, not `"default"`
- [ ] Show a namespace picker in the create-space modal
- [ ] Preload options from the user's known namespaces

### 2. Namespace Discovery

Add an endpoint that returns namespaces the user actually has data in.

**Endpoint:** `GET /api/v1/namespaces`

**Sources:**
- Existing spaces (their project_ids)
- Memories grouped by project_id

**Response:**
```json
{
  "namespaces": [
    { "project_id": "clawdbot", "memory_count": 304, "has_space": true },
    { "project_id": "default", "memory_count": 0, "has_space": false }
  ],
  "active": "clawdbot"
}
```

**Do not force users to type namespaces blindly.**

### 3. Empty-State Guardrail

If selected space namespace has 0 memories but the user has memories in other namespaces:

- [ ] Show a warning: "This namespace has no memories"
- [ ] Offer "Switch to existing namespace" action
- [ ] Offer "Relink this space" action

### 4. Migration Path

For existing spaces pointing to `default` with no data:

- [ ] Prompt user to relink to a populated namespace
- [ ] **Do not silently rewrite spaces** — user must confirm

### 5. Visibility

Show active namespace clearly in:

- [ ] Project switcher dropdown
- [ ] Space details panel
- [ ] Settings / session panel

---

## What NOT To Do

**Do not collapse everything to one namespace per user.**

- That weakens project isolation
- It fights the enterprise model (multi-project, multi-tenant)
- Better defaulting and discovery is the right fix

---

## Acceptance Criteria

1. [ ] If a user has memories in `clawdbot`, creating a new space defaults to `clawdbot`
2. [ ] The create-space modal shows real namespace options (from discovery endpoint)
3. [ ] A user can't end up on an empty space silently if populated namespaces exist
4. [ ] Existing mismatched spaces can be relinked without data loss
5. [ ] The switcher always persists `namespace`, never `space.id` (see BUG-001)

---

## Implementation Plan

### Phase 1: Backend

**File:** `src/remembra/api/v1/namespaces.py` (new)

```python
@router.get("/namespaces")
async def list_namespaces(user: User = Depends(get_current_user)):
    """Return all namespaces with memory counts for the user."""
    # Query memories grouped by project_id
    # Query existing spaces
    # Return merged list with counts and has_space flags
```

### Phase 2: Dashboard - Create Space

**File:** `dashboard/src/pages/Projects.tsx`

- Fetch namespaces on mount
- Replace text input with dropdown
- Default to namespace with most memories

### Phase 3: Dashboard - Empty State

**File:** `dashboard/src/components/EmptyState.tsx`

- Check if current namespace has 0 memories
- Check if other namespaces have memories
- Show relink prompt if mismatch detected

### Phase 4: Dashboard - Visibility

**Files:**
- `dashboard/src/components/ProjectSwitcher.tsx` — show namespace badge
- `dashboard/src/pages/SpaceDetails.tsx` — show namespace in details
- `dashboard/src/pages/Settings.tsx` — show active namespace

---

## Database Queries

### Get user's namespaces with counts:

```sql
SELECT 
  project_id,
  COUNT(*) as memory_count
FROM memories
WHERE user_id = ?
GROUP BY project_id
ORDER BY memory_count DESC;
```

### Get spaces with their namespaces:

```sql
SELECT 
  id,
  name,
  project_id
FROM memory_spaces
WHERE owner_id = ?;
```

### Detect orphaned memories (no space pointing to them):

```sql
SELECT DISTINCT m.project_id
FROM memories m
WHERE m.user_id = ?
  AND m.project_id NOT IN (
    SELECT project_id FROM memory_spaces WHERE owner_id = ?
  );
```

---

## Testing Checklist

- [ ] New user stores memories via CLI → creates space → space defaults to correct namespace
- [ ] User with memories in `clawdbot` → create space modal shows `clawdbot` as option
- [ ] User selects empty namespace → warning shown with relink option
- [ ] User relinks space → memories appear, no data loss
- [ ] Project switcher shows namespace, not UUID
- [ ] Settings panel shows active namespace

---

## Related

- BUG-001: ProjectSwitcher State Bug (UI layer)
- This bug is the product/data-model layer

---

## Notes

Discovered when Mani's dashboard appeared empty despite 304 memories existing. Root cause: memories in `clawdbot` namespace, spaces pointing to `default` namespace.

Manual fix applied: `UPDATE memory_spaces SET project_id = 'clawdbot' WHERE project_id = 'default'`

This is not a product fix — proper namespace discovery and defaulting is required.
