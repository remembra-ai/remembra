# BUG: Project Namespace Mismatch Between Memories and Spaces

**Severity:** High  
**Discovered:** 2026-03-16  
**Status:** Needs Fix  
**Affects:** All users who store memories via API/CLI and use dashboard

---

## Summary

Users can have memories stored under one `project_id` (e.g., `clawdbot`) while their dashboard spaces point to a different `project_id` (e.g., `default`). This causes the dashboard to appear empty even when hundreds of memories exist.

---

## Symptoms

- Dashboard shows "0 memories" despite API confirming memories exist
- Knowledge Graph may show entities but Memories list is empty
- Projects page shows spaces with "0 memories"
- User believes data was wiped/lost

---

## Root Cause

### The Disconnect

1. **Memories are stored with a `project_id`** - determined by:
   - API client configuration
   - CLI default settings
   - Agent configuration (e.g., Clawdbot uses `clawdbot`)

2. **Spaces are created with a `project_id`** - determined by:
   - Dashboard defaults to `default`
   - User has no visibility into existing memory namespaces
   - No validation that the project_id matches any existing memories

3. **Dashboard queries memories by space's `project_id`** - if they don't match, nothing shows up.

### Example Scenario (Actual Bug)

```
Memories stored:     project_id = 'clawdbot' (304 memories)
Space "Audit Test":  project_id = 'default'  (0 memories found)

Result: Dashboard appears empty
```

---

## Current Workaround

Manually update space records in SQLite:

```sql
UPDATE memory_spaces 
SET project_id = 'clawdbot' 
WHERE project_id = 'default';
```

This is not a product fix.

---

## Proper Fix Required

### Option A: Auto-Detection (Recommended)

When creating a space, the UI should:

1. Query existing memory namespaces:
   ```sql
   SELECT DISTINCT project_id, COUNT(*) as count 
   FROM memories 
   WHERE user_id = ? 
   GROUP BY project_id
   ```

2. Present options to user:
   - "You have memories in these namespaces: clawdbot (304), default (0)"
   - "Which namespace should this space connect to?"

3. Default to the namespace with the most memories

### Option B: Unified Namespace

Simplify the model:
- Each user gets ONE default namespace
- All memories go there unless explicitly specified
- Spaces don't have separate project_ids - they're views into the user's memories
- Projects become organizational tags, not data silos

### Option C: Namespace Discovery Page

Add a "Data Sources" or "Namespaces" page that shows:
- All project_ids with memory counts
- Which spaces are connected to which namespaces
- Ability to re-map spaces to different namespaces
- Warning if a namespace has no connected space

---

## Implementation Tasks

### Phase 1: Immediate (Fix Data Visibility)

- [ ] Add API endpoint: `GET /api/v1/namespaces` - lists all project_ids with memory counts for user
- [ ] Add "Namespace" dropdown to space creation form
- [ ] Pre-populate with discovered namespaces
- [ ] Warn if selected namespace has 0 memories

### Phase 2: UX Improvements

- [ ] Add namespace indicator to Projects page (show which project_id each space uses)
- [ ] Add "Re-map Namespace" action to space settings
- [ ] Add "Orphaned Memories" warning if user has memories in namespaces with no space

### Phase 3: Architecture Review

- [ ] Consider whether spaces and project_ids should be unified
- [ ] Consider whether project_id should be auto-generated or user-specified
- [ ] Document the mental model clearly for users

---

## Files Affected

- `dashboard/src/components/ProjectSwitcher.tsx` - already has partial fix for localStorage
- `dashboard/src/pages/Projects.tsx` - needs namespace discovery
- `src/remembra/api/v1/spaces.py` - needs namespace listing endpoint
- `src/remembra/spaces/manager.py` - needs validation logic

---

## Testing Checklist

After fix is implemented:

- [ ] New user creates account, stores memories via CLI → sees them in dashboard
- [ ] User creates space with custom project_id → correctly links to memories
- [ ] User with existing memories in multiple namespaces → can see and select them
- [ ] Space creation warns if selected namespace has no memories
- [ ] Existing users' data remains accessible

---

## Related Issues

- ProjectSwitcher UUID detection fix (b5a1a46) - handles corrupted localStorage but doesn't solve namespace mismatch
- Dashboard "empty" bug reports - likely all caused by this root issue

---

## Notes

This bug was discovered when Mani's dashboard appeared empty despite having 304 memories. Investigation revealed:

```
Memories: project_id = 'clawdbot' (304 count)
Spaces: project_id = 'default' (0 count in that namespace)
```

Manual database update fixed the immediate issue, but product needs architectural fix.
