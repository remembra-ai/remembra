import { describe, expect, it } from 'vitest';
import { ApiError } from '../api';
import { explainError } from '../relay';
import { agentMeta, canonicalAgentId } from '../agents';
import { isPermanentFailure } from '../../hooks/useResource';

describe('explainError', () => {
  it('explains each failure with a fix', () => {
    expect(explainError(new ApiError('Could not reach the Remembra server', 0), 'the trail').title).toMatch(/Couldn't reach/);
    expect(explainError(new ApiError('Invalid token', 401), 'the trail').fix).toMatch(/sign in again/i);
    expect(explainError(new ApiError('Permission denied', 403), 'the trail').fix).toMatch(/viewer role/);
    expect(explainError(new ApiError('Not Found', 404), 'the trail').fix).toMatch(/Update Remembra/);
    expect(explainError(new ApiError('Agent inbox is not available', 503), 'the inbox').title).toBe(
      'The inbox is turned off on this server.',
    );
    expect(explainError(new ApiError('Bad gateway', 502), 'the trail').title).toMatch(/isn't answering/);
  });

  it('keeps a useful server message on 500s and hides the generic one', () => {
    expect(explainError(new ApiError('API error: 500', 500), 'the trail').fix).not.toContain('API error');
    expect(explainError(new ApiError('database is locked', 500), 'the trail').fix).toContain('database is locked');
  });

  it('handles non-API errors', () => {
    expect(explainError(new Error('boom'), 'the trail')).toEqual({ title: "Couldn't load the trail.", fix: 'boom' });
  });
});

describe('agents', () => {
  it('maps known ids and aliases to display names', () => {
    expect(agentMeta('claude-code').name).toBe('Claude Code');
    expect(canonicalAgentId('Claude')).toBe('claude-code');
    expect(agentMeta('gemini-cli').name).toBe('Gemini CLI');
  });

  it('derives a stable name, monogram and lane for unknown agents', () => {
    const meta = agentMeta('trademind-bot');
    expect(meta.name).toBe('Trademind Bot');
    expect(meta.monogram).toBe('TB');
    expect(agentMeta('trademind-bot').lane).toBe(meta.lane);
    expect(agentMeta(null).name).toBe('Unattributed');
  });
});

describe('isPermanentFailure', () => {
  it('stops polling only for failures a retry cannot fix', () => {
    for (const status of [401, 403, 404, 503]) expect(isPermanentFailure(new ApiError('x', status))).toBe(true);
    for (const status of [0, 429, 500, 502, 504]) expect(isPermanentFailure(new ApiError('x', status))).toBe(false);
    expect(isPermanentFailure(new Error('network'))).toBe(false);
  });
});
