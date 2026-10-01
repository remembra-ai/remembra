import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { WorkReport } from '../OpenWork';
import type { OpenWorkItem } from '../../../lib/continuity';

describe('reported open work', () => {
  it('keeps a proposed resolution unresolved and renders reports as inert text', () => {
    const item: OpenWorkItem = {
      id: 'a', project_id: 'widget', kind: 'failure', state: 'resolution_proposed',
      text: '<img src="https://external.invalid/leak">', version: 2, source_handoff_id: 'h&1',
      resolution_memory_id: 'proof', resolution_digest: 'digest', evidence_available: true,
      facts_source: 'agent-declared', trust_score: 0.2, withheld: true, flags: [],
    };
    const html = renderToStaticMarkup(<WorkReport item={item} projectId="widget" />);
    expect(html).toContain('resolution awaiting review');
    expect(html).toContain('Reported by an agent');
    expect(html).toContain('Withheld from briefs');
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
    expect(html).toContain('open=h%261');
  });
});
