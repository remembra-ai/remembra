import { describe, expect, it } from 'vitest';
import {
  CLIENT_MSG_ID_RE,
  activeCommand,
  activeMention,
  applyMention,
  authorOf,
  canEdit,
  decisionAuthor,
  deliveryLines,
  filterMentions,
  groupThreads,
  mentionOptions,
  mergeMessages,
  newClientMsgId,
  parseComposer,
  parseMentions,
  splitDecisions,
  when,
  type ChannelMessage,
} from '../channel/model';
import { crewState, session } from './fixtures';

function msg(id: string, seq: number, extra: Partial<ChannelMessage> = {}): ChannelMessage {
  return {
    id,
    seq,
    kind: 'chat',
    author_kind: 'agent',
    author_session_id: 'cs_a',
    author_agent_id: 'claude-code',
    author_verified: true,
    body: `body ${id}`,
    body_truncated: false,
    mentions: [],
    refs: [],
    edited: false,
    redacted: false,
    pinned: false,
    ...extra,
  };
}

describe('mentions (server grammar)', () => {
  it('parses like channel.py parse_mentions: order, lowercase, dedupe, trailing punctuation, boundaries', () => {
    expect(parseMentions('Hey @CC-1, and @crew. Also @zone:pos: and @task:T-14 @cc-1')).toEqual(['cc-1', 'crew', 'zone:pos', 'task:t-14']);
    expect(parseMentions('mail me at mani@example.com or a/@b or x.@y')).toEqual([]);
    expect(parseMentions('@-bad @ok')).toEqual(['ok']);
    const many = Array.from({ length: 30 }, (_, i) => `@a${i}`).join(' ');
    expect(parseMentions(many)).toHaveLength(20);
  });

  it('finds the mention being typed at the caret and ignores emails', () => {
    expect(activeMention('ping @co', 8)).toEqual({ start: 5, query: 'co' });
    expect(activeMention('@', 1)).toEqual({ start: 0, query: '' });
    expect(activeMention('mani@exa', 8)).toBeNull();
    expect(activeMention('@cc-1 done', 10)).toBeNull();
  });

  it('offers live callsigns, agents, crew, zones and open tasks, not ended sessions or the policy zone', () => {
    const state = crewState();
    const tokens = mentionOptions(state).map((o) => o.token);
    expect(tokens).toContain('cc-1');
    expect(tokens).toContain('codex-1');
    expect(tokens).toContain('gemini-1');
    expect(tokens).not.toContain('cc-3');
    expect(tokens).toContain('codex');
    expect(tokens).toContain('crew');
    expect(tokens).toContain('zone:pos');
    expect(tokens).not.toContain('zone:crew-policy');
    expect(tokens).toContain('task:T-14');
    expect(tokens).not.toContain('task:T-1');
    const hint = mentionOptions(state).find((o) => o.token === 'codex-1')?.hint;
    expect(hint).toContain('at its next MCP call or session start');
    expect(filterMentions(mentionOptions(state), 'zo').map((o) => o.token)).toEqual(['zone:pos', 'zone:reports']);
  });

  it('replaces the typed mention with the chosen token and puts the caret after it', () => {
    expect(applyMention('ask @co about it', 7, 'codex-1')).toEqual({ text: 'ask @codex-1 about it', caret: 13 });
    expect(applyMention('@c', 2, 'crew')).toEqual({ text: '@crew ', caret: 6 });
  });
});

describe('composer intents', () => {
  const zones = Object.values(crewState().zones);

  it('plain text posts with the chosen kind', () => {
    expect(parseComposer('  hello  ', zones, 'question')).toEqual({ type: 'message', kind: 'question', body: 'hello' });
    expect(parseComposer('   ', zones, 'chat')).toEqual({ type: 'empty' });
  });

  it('/decide needs text and titles the decision by its first line', () => {
    expect(parseComposer('/decide', zones, 'chat')).toMatchObject({ type: 'invalid' });
    expect(parseComposer('/decide GCT rounds half-up\nper line, always', zones, 'chat')).toEqual({
      type: 'decide',
      body: 'GCT rounds half-up\nper line, always',
      title: 'GCT rounds half-up',
    });
  });

  it('/freeze needs a real, unfrozen, non-policy zone and a reason', () => {
    expect(parseComposer('/freeze', zones, 'chat')).toMatchObject({ type: 'invalid', error: expect.stringContaining('pos, reports') });
    expect(parseComposer('/freeze billing now', zones, 'chat')).toMatchObject({ type: 'invalid', error: expect.stringContaining('No zone "billing"') });
    expect(parseComposer('/freeze crew-policy x', zones, 'chat')).toMatchObject({ type: 'invalid', error: expect.stringContaining('always protected') });
    expect(parseComposer('/freeze pos', zones, 'chat')).toMatchObject({ type: 'invalid', error: expect.stringContaining('Add a reason') });
    const ok = parseComposer('/freeze zone:POS I am editing POS myself', zones, 'chat');
    expect(ok).toMatchObject({ type: 'freeze', reason: 'I am editing POS myself' });
    expect(ok.type === 'freeze' && ok.zone.id).toBe('zn_pos');
    const frozen = zones.map((z) => (z.slug === 'pos' ? { ...z, frozen_by: 'u_mani' } : z));
    expect(parseComposer('/freeze pos again', frozen, 'chat')).toMatchObject({ type: 'invalid', error: expect.stringContaining('already frozen') });
  });

  it('unknown commands and oversize bodies are refused before sending', () => {
    expect(parseComposer('/deploy now', zones, 'chat')).toMatchObject({ type: 'invalid', error: expect.stringContaining('Unknown command /deploy') });
    expect(parseComposer('é'.repeat(4097), zones, 'chat')).toMatchObject({ type: 'invalid' }); // 8,194 bytes
    expect(parseComposer('a'.repeat(8192), zones, 'chat')).toMatchObject({ type: 'message' });
  });

  it('shows slash commands only while the command word is typed', () => {
    expect(activeCommand('/de', 3)).toBe('de');
    expect(activeCommand('/decide x', 9)).toBeNull();
    expect(activeCommand('hi /de', 6)).toBeNull();
  });

  it('client message ids match the server grammar', () => {
    for (let i = 0; i < 20; i++) expect(newClientMsgId()).toMatch(CLIENT_MSG_ID_RE);
  });
});

describe('delivery expectation', () => {
  const state = crewState();

  it('says when each addressed session sees the message (hooked: next turn; advisory/MCP: next MCP call)', () => {
    expect(deliveryLines('@cc-1 @codex-1 please', state)).toEqual([
      'cc-1 will see this at its next turn.',
      'codex-1 (advisory) will see this at its next MCP call or session start.',
    ]);
  });

  it('routes zones to holders, tasks to owners, and dedupes', () => {
    expect(deliveryLines('@zone:pos @task:T-14 @cc-1', state)).toEqual(['cc-1 will see this at its next turn.']);
    expect(deliveryLines('@zone:reports', state)).toEqual(['Nobody holds zone reports right now: nobody is notified for it.']);
    expect(deliveryLines('@zone:billing', state)).toEqual(['No zone "billing" in this crew: nobody is notified for it.']);
    expect(deliveryLines('@task:T-2', state)).toEqual(['T-2 has no live owner: nobody is notified for it.']);
    expect(deliveryLines('@task:T-99', state)).toEqual(['No task T-99 in this crew.']);
  });

  it('labels self-declared sessions reached through @agent, and stopped sessions', () => {
    const lines = deliveryLines('@claude-code', state);
    expect(lines).toContain('cc-1 will see this at its next turn.');
    expect(lines).toContain('cc-9 will see this at its next turn (labelled "addressed to claude-code, you are self-declared").');
    expect(deliveryLines('@gemini-1', state)).toEqual(['gemini-1 will see this only if it comes back (it stopped).']);
  });

  it('covers @crew, human aliases, agents with no live session, unknown names and no mentions', () => {
    expect(deliveryLines('@crew standup', state)[0]).toMatch(/^Every live agent \(4\)/);
    expect(deliveryLines('@mani', state)[0]).toContain('is you');
    const s2 = crewState({ sessions: [session('cs_x', 'qwen-1', { agent_id: 'qwen', state: 'ended' })] });
    expect(deliveryLines('@qwen', s2)).toEqual(["No qwen session is live: it goes to qwen's agent inbox and shows in its next session brief, after the last session."]);
    expect(deliveryLines('@nobody', state)).toEqual(['@nobody matches no live agent, zone or task: nobody is notified for it.']);
    expect(deliveryLines('just a note', state)[0]).toMatch(/^Nobody is mentioned/);
  });

  it('when() covers paused and quiet sessions', () => {
    expect(when({ adapter_enforcement: 'enforced', client_kind: 'hook', state: 'paused' })).toBe('when it is resumed');
    expect(when({ adapter_enforcement: 'enforced', client_kind: 'hook', state: 'quiet' })).toBe('when it is heard from again');
  });
});

describe('threads and messages', () => {
  it('merges REST rows and live events by id, keeping the full body over a clipped event body', () => {
    const full = msg('m1', 1, { body: 'x'.repeat(5000), created_at: '2026-09-26T10:00:00Z', author_label: 'agent claude-code (key-verified)' });
    const event = msg('m1', 1, { body: 'x'.repeat(4000), body_truncated: true });
    const merged = mergeMessages([full], [event, msg('m2', 2)]);
    expect(merged.map((m) => m.id)).toEqual(['m1', 'm2']);
    expect(merged[0].body).toHaveLength(5000);
    expect(merged[0].created_at).toBe('2026-09-26T10:00:00Z');
    const edited = mergeMessages([full], [{ ...event, edited: true, body: 'new text' }]);
    expect(edited[0].body).toBe('new text');
  });

  it('groups replies under roots, newest activity first, flags unanswered questions', () => {
    const messages = [
      msg('q1', 1, { kind: 'question' }),
      msg('c1', 2),
      msg('r1', 3, { thread_root_id: 'c1', author_session_id: 'cs_b' }),
      msg('q2', 4, { kind: 'question' }),
      msg('a2', 5, { thread_root_id: 'q2', kind: 'answer', author_kind: 'human', author_session_id: null }),
      msg('orphan', 6, { thread_root_id: 'old_root' }),
    ];
    const threads = groupThreads(messages, (m) => m.author_session_id ?? 'human');
    expect(threads.map((t) => t.id)).toEqual(['old_root', 'q2', 'c1', 'q1']);
    expect(threads.find((t) => t.id === 'q1')?.openQuestion).toBe(true);
    expect(threads.find((t) => t.id === 'q2')?.openQuestion).toBe(false);
    expect(threads.find((t) => t.id === 'c1')?.participants).toEqual(['cs_a', 'cs_b']);
    expect(threads.find((t) => t.id === 'old_root')?.root).toBeNull();
  });

  it('names authors with provenance and allows edits only by the author within 10 minutes', () => {
    const state = crewState();
    expect(authorOf(msg('a', 1), state, 'u1')).toMatchObject({ name: 'cc-1', provenance: 'key-verified', agentId: 'claude-code' });
    expect(authorOf(msg('b', 1, { author_session_id: 'cs_c', author_verified: false }), state, 'u1')).toMatchObject({ name: 'cc-9', provenance: 'self-declared' });
    const mine = msg('h', 1, { author_kind: 'human', author_user_id: 'u1', created_at: '2026-09-26T12:00:00Z' });
    expect(authorOf(mine, state, 'u1')).toMatchObject({ name: 'you', provenance: 'you' });
    expect(authorOf(mine, state, 'u2')).toMatchObject({ name: 'human' });
    expect(canEdit(mine, 'u1', new Date('2026-09-26T12:09:00Z'))).toBe(true);
    expect(canEdit(mine, 'u1', new Date('2026-09-26T12:11:00Z'))).toBe(false);
    expect(canEdit(mine, 'u2', new Date('2026-09-26T12:01:00Z'))).toBe(false);
    expect(canEdit({ ...mine, redacted: true }, 'u1', new Date('2026-09-26T12:01:00Z'))).toBe(false);
  });

  it('splits decisions and names who proposed them', () => {
    const state = crewState();
    const { inForce, proposed } = splitDecisions(state.decisions);
    expect(inForce.map((d) => d.number)).toEqual([7]);
    expect(proposed.map((d) => d.number)).toEqual([9]);
    expect(decisionAuthor(proposed[0], state)).toBe('codex-1 · codex (key-verified)');
    expect(decisionAuthor(inForce[0], state)).toBe('a human');
  });
});
