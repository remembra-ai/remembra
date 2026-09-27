import { afterEach, describe, expect, it, vi } from 'vitest';

import { Remembra, RemembraError, ValidationError } from '../src/index';

// forget() maps to DELETE /api/v1/memories. Only allMemories: true may ask
// for the whole account; nothing else can turn into that.

function client() {
  return new Remembra({ url: 'http://localhost:8787', userId: 'user_123' });
}

function json(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

// Records the DELETEs; GET /health answers `health`.
function recordDeletes(health: Record<string, unknown> = { status: 'ok', version: '0.16.1' }) {
  const urls: URL[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: { method: string }) => {
      if (init.method === 'GET') {
        expect(new URL(url).pathname).toBe('/health');
        return json(health);
      }
      expect(init.method).toBe('DELETE');
      urls.push(new URL(url));
      return json({ deleted_memories: 2, deleted_entities: 1, deleted_relationships: 0 });
    }),
  );
  return urls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('forget', () => {
  it('sends one memory id', async () => {
    const urls = recordDeletes();
    await client().forget({ memoryId: 'mem_123' });
    expect(urls[0].pathname).toBe('/api/v1/memories');
    expect(Object.fromEntries(urls[0].searchParams)).toEqual({ memory_id: 'mem_123' });
  });

  it('sends an entity, limited to a project when given, and returns the counts', async () => {
    const urls = recordDeletes();
    const result = await client().forget({ entity: ' John ', projectId: 'work' });
    expect(Object.fromEntries(urls[0].searchParams)).toEqual({ entity: 'John', project_id: 'work' });
    expect(result).toEqual({ deleted_memories: 2, deleted_entities: 1, deleted_relationships: 0 });

    await client().forget({ entity: 'John' });
    expect(Object.fromEntries(urls[1].searchParams)).toEqual({ entity: 'John' });
  });

  it('asks for the whole account only with allMemories: true', async () => {
    const urls = recordDeletes();
    await client().forget({ allMemories: true });
    expect(Object.fromEntries(urls[0].searchParams)).toEqual({ all_memories: 'true' });
  });

  it('does not send an entity delete to a server that would wipe the account', async () => {
    for (const version of ['0.16.0', '0.9.9', '', undefined, 'unknown']) {
      const urls = recordDeletes({ status: 'ok', version }); // undefined: no version reported
      const error = await client()
        .forget({ entity: 'John' })
        .catch((e: unknown) => e);
      expect(error).toBeInstanceOf(RemembraError);
      expect((error as RemembraError).code).toBe('SERVER_TOO_OLD');
      expect(urls).toEqual([]);
    }
  });

  it('sends nothing without exactly one target', async () => {
    const urls = recordDeletes();
    const memory = client();
    const bad: unknown[] = [
      undefined,
      {},
      { allMemories: false },
      { entity: '   ' },
      { memoryId: 'mem_1', entity: 'John' },
      { entity: 'John', allMemories: true },
      { projectId: 'work' },
      { memoryId: 'mem_1', projectId: 'work' },
      { entity: 'John', projectId: ' ' },
    ];
    for (const options of bad) {
      await expect(memory.forget(options as never)).rejects.toBeInstanceOf(ValidationError);
    }
    expect(urls).toEqual([]);
  });
});
