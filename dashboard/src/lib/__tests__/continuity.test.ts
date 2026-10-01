import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api';
import { continuity, loadOpenWork, type OpenWorkItem } from '../continuity';

const item = (id: string, project_id = 'widget'): OpenWorkItem => ({
  id, project_id, kind: 'failure', state: 'resolution_proposed', text: 'A failed checkout', version: 2,
  source_handoff_id: 'handoff', resolution_memory_id: 'proof', resolution_digest: 'digest',
  evidence_available: true, facts_source: 'agent-declared',
});

afterEach(() => vi.restoreAllMocks());

describe('persistent work paging and confirmation', () => {
  it('reloads all visible pages instead of retaining resolved older items', async () => {
    const list = vi.spyOn(continuity, 'list')
      .mockResolvedValueOnce({ project_id: 'widget', total: 2, items: [item('a')], next_after: 'a' })
      .mockResolvedValueOnce({ project_id: 'widget', total: 2, items: [item('b')], next_after: null })
      .mockResolvedValueOnce({ project_id: 'widget', total: 1, items: [item('b')], next_after: null });
    expect((await loadOpenWork('widget', 2)).items.map((v) => v.id)).toEqual(['a', 'b']);
    expect(list).toHaveBeenNthCalledWith(2, 'widget', 'a');
    expect((await loadOpenWork('widget', 2)).items.map((v) => v.id)).toEqual(['b']);
  });

  it.each(['page', 'item'])('rejects cross-project %s data before displaying it', async (where) => {
    vi.spyOn(continuity, 'list').mockResolvedValue({
      project_id: where === 'page' ? 'other' : 'widget', total: 1,
      items: [item('a', where === 'item' ? 'other' : 'widget')], next_after: null,
    });
    await expect(loadOpenWork('widget', 1)).rejects.toThrow(/different project/);
  });

  it('refuses a repeated cursor rather than presenting an incomplete list as complete', async () => {
    vi.spyOn(continuity, 'list').mockResolvedValue({ project_id: 'widget', total: 4, items: [item('a')], next_after: 'a' });
    await expect(loadOpenWork('widget', 3)).rejects.toThrow(/repeated/);
  });

  it('submits the reviewed version and propagates evidence conflicts', async () => {
    const request = vi.spyOn(api, 'request').mockRejectedValue(new ApiError('Evidence changed', 409));
    await expect(continuity.confirm('widget', item('a'))).rejects.toMatchObject({ status: 409 });
    expect(request).toHaveBeenCalledWith('/session/open-work/a', {
      method: 'POST', body: JSON.stringify({ project_id: 'widget', version: 2, action: 'confirm_resolution' }),
    });
  });

  it('encodes the explicit project instead of selecting a configured default', async () => {
    const request = vi.spyOn(api, 'request').mockResolvedValue({});
    await continuity.list('widget & other', 'a'.repeat(40));
    expect(request.mock.calls[0][0]).toContain('project_id=widget+%26+other');
  });
});
