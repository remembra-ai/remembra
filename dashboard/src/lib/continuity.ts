import { api } from './api';

export interface OpenWorkItem {
  id: string;
  project_id: string;
  kind: 'todo' | 'failure';
  state: 'open' | 'resolution_proposed';
  text: string;
  version: number;
  source_handoff_id: string;
  resolution_memory_id: string | null;
  resolution_digest: string | null;
  evidence_available: boolean;
  facts_source: string;
  trust_score?: number;
  withheld?: boolean;
  flags?: string[];
}

export interface OpenWorkPage {
  project_id: string;
  total: number;
  items: OpenWorkItem[];
  next_after: string | null;
}

export const continuity = {
  list(projectId: string, after = '') {
    const query = new URLSearchParams({ project_id: projectId, limit: '25', after });
    return api.request<OpenWorkPage>(`/session/open-work?${query}`);
  },
  confirm(projectId: string, item: OpenWorkItem) {
    return api.request<{ id: string; state: string; version: number }>(
      `/session/open-work/${encodeURIComponent(item.id)}`,
      { method: 'POST', body: JSON.stringify({ project_id: projectId, version: item.version, action: 'confirm_resolution' }) },
    );
  },
};

/** Reload every visible page so polling cannot retain an externally resolved row. */
export async function loadOpenWork(projectId: string, pages: number): Promise<OpenWorkPage> {
  if (!Number.isInteger(pages) || pages < 1) throw new Error('At least one page is required.');
  const items = new Map<string, OpenWorkItem>();
  let after = '';
  let result: OpenWorkPage;
  const seen = new Set<string>();
  for (let n = 0; n < pages; n++) {
    result = await continuity.list(projectId, after);
    if (result.project_id !== projectId) throw new Error('The server returned work from a different project.');
    for (const item of result.items) {
      if (item.project_id !== projectId) throw new Error('An open-work item belongs to a different project.');
      items.set(item.id, item);
    }
    if (!result.next_after) return { ...result, items: [...items.values()] };
    if (seen.has(result.next_after)) throw new Error('The server repeated its page cursor. Refresh to try again.');
    seen.add(result.next_after);
    after = result.next_after;
  }
  return { ...result!, items: [...items.values()] };
}
