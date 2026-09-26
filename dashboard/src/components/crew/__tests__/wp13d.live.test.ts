/// <reference types="node" />
// WP-13d live check: Channel, Inbox tabs, decision confirm, notifications
// and notify targets against a real Remembra server (real FastAPI app, real
// crew.db, real /ws, real JWT). Driven by tests/crew/test_dashboard_wp13d_live.py,
// which starts the server, seeds a crew with agent activity and sets
// CREW_LIVE_URL / CREW_LIVE_JWT / CREW_LIVE_KEY / CREW_LIVE_CREW. Skipped otherwise.
//
// Every step runs the code the screens run: the crew API client, the pure
// models (composer intents, delivery lines, threads, primary actions, sort,
// session queues, notification cursors, target checks) and the live store
// over the shared socket.

import { writeFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { CrewApiError, createCrewApi } from '../../../lib/crew/api';
import { fromSnapshot } from '../../../lib/crew/reducer';
import { CrewRuntime } from '../../../lib/crew/runtime';
import { crewSocketUrl, type SocketLike } from '../../../lib/crew/socket';
import type { CrewEvent, DecisionView } from '../../../lib/crew/types';
import {
  deliveryLines,
  groupThreads,
  mergeMessages,
  newClientMsgId,
  parseComposer,
  splitDecisions,
  type ChannelMessage,
} from '../channel/model';
import { actionError } from '../channel/useChannel';
import { foldSessionQueues, originLabel, primaryAction, sortItems, type InboxItem } from '../inbox/model';
import { targetProblem, unreadUpto, type AddedTarget, type NotificationList, type NotificationRules } from '../notify/model';

const URL_ = process.env.CREW_LIVE_URL ?? '';
const JWT = process.env.CREW_LIVE_JWT ?? '';
const KEY = process.env.CREW_LIVE_KEY ?? '';
const CREW = process.env.CREW_LIVE_CREW ?? '';
const OUT = process.env.CREW_LIVE_OUT ?? '';

async function waitFor(what: string, check: () => boolean, timeoutMs = 10000): Promise<void> {
  const end = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 25));
  }
}

describe.skipIf(!URL_)('WP-13d against a live crew server', () => {
  it('channel, decisions, inbox tabs, notifications and targets work end to end', async () => {
    const api = createCrewApi({ baseUrl: URL_, credentials: () => ({ jwt: JWT }), fetch: (u, i) => fetch(u, i) });
    const keyApi = createCrewApi({ baseUrl: URL_, credentials: () => ({ apiKey: KEY }), fetch: (u, i) => fetch(u, i) });
    const runtime = new CrewRuntime({
      api,
      socketUrl: crewSocketUrl(URL_),
      credentials: () => ({ jwt: JWT }),
      createSocket: (url) => new WebSocket(url) as unknown as SocketLike,
      lingerMs: 100,
    });
    const report: Record<string, unknown> = {};
    const nameOf = (m: ChannelMessage) => m.author_callsign ?? m.author_kind;
    try {
      // -- who may act: the dashboard login is human, an admin API key never is (D27) ---------------
      expect((await api.getCrew(CREW)).human).toBe(true);
      expect((await keyApi.getCrew(CREW)).human).toBe(false);

      const release = runtime.leaseCrew(CREW);
      const store = runtime.storeFor(CREW);
      await waitFor('live stream', () => store.getView().status === 'live');
      const state = () => store.getView().state!;

      // -- the channel window: the seeded agent traffic, grouped into threads ----------------------
      const first = await api.messages(CREW, { before: Number.MAX_SAFE_INTEGER, limit: 200 });
      let messages = mergeMessages([], first.items as ChannelMessage[]);
      expect(messages).toHaveLength(4);
      const question = messages.find((m) => m.kind === 'question')!;
      expect(question).toMatchObject({ author_callsign: 'codex-1', author_label: 'agent codex (key-verified)' });
      let threads = groupThreads(messages, nameOf);
      expect(threads.find((t) => t.id === question.id)?.openQuestion).toBe(true);

      // -- composer: delivery lines agree with the server's routing --------------------------------
      const text = '@cc-1 @codex-1 hold POS until T-14 lands';
      const intent = parseComposer(text, Object.values(state().zones), 'chat');
      expect(intent).toEqual({ type: 'message', kind: 'chat', body: text });
      const lines = deliveryLines(text, state());
      expect(lines).toEqual(['cc-1 will see this at its next turn.', 'codex-1 (advisory) will see this at its next MCP call or session start.']);
      const clientMsgId = newClientMsgId();
      const posted = await api.postMessage(CREW, { kind: 'chat', body: text, clientMsgId });
      const routed = (posted.routing as { sessions: string[] }).sessions;
      expect(routed.map((sid) => state().sessions[sid]?.callsign).sort()).toEqual(['cc-1', 'codex-1']);
      // a retry of the same draft never posts twice
      const again = await api.postMessage(CREW, { kind: 'chat', body: text, clientMsgId });
      expect(again.replayed).toBe(true);
      expect((again.message as ChannelMessage).id).toBe((posted.message as ChannelMessage).id);

      // -- the live stream delivers it; a long body keeps its full REST text after the clipped event --
      const long = `status: ${'x'.repeat(4500)}`;
      const longPost = await api.postMessage(CREW, { kind: 'note', body: long, clientMsgId: newClientMsgId() });
      const longId = (longPost.message as ChannelMessage).id;
      await waitFor('message.posted over the socket', () => state().messages.some((m) => m.id === longId));
      const event = state().messages.find((m) => m.id === longId)!;
      expect(event.body_truncated).toBe(true);
      const rest = await api.messages(CREW, { since_seq: 0, limit: 200 });
      messages = mergeMessages(mergeMessages(messages, rest.items as ChannelMessage[]), state().messages as ChannelMessage[]);
      expect(messages.find((m) => m.id === longId)?.body).toBe(long);

      // -- reply in the question's thread (kind answer): the thread is no longer an open question ---
      await api.postMessage(CREW, { kind: 'answer', body: 'Round per line, half-up.', thread_root_id: question.id, clientMsgId: newClientMsgId() });
      const thread = await api.messages(CREW, { thread: question.id });
      expect(thread.items).toHaveLength(2);
      messages = mergeMessages(messages, thread.items as ChannelMessage[]);
      threads = groupThreads(messages, nameOf);
      expect(threads.find((t) => t.id === question.id)?.openQuestion).toBe(false);

      // -- /decide: a human decision is in force at once --------------------------------------------
      const decide = parseComposer('/decide GCT rounds half-up per line', [], 'chat');
      expect(decide.type).toBe('decide');
      const decided = await api.postMessage(CREW, { kind: 'decision', body: decide.type === 'decide' ? decide.body : '', clientMsgId: newClientMsgId() });
      expect((decided.decision as DecisionView).state).toBe('in_force');
      await waitFor('decision in state', () => Object.values(state().decisions).some((d) => d.title === 'GCT rounds half-up per line'));

      // -- the agent-proposed decision: an API key cannot confirm, the human can ---------------------
      const { proposed } = splitDecisions(state().decisions);
      expect(proposed).toHaveLength(1);
      const byKey = await keyApi.confirmDecision(proposed[0].id).catch((e: unknown) => e);
      expect(byKey).toBeInstanceOf(CrewApiError);
      expect((byKey as CrewApiError).status).toBe(403);
      report.key_confirm_error = actionError(byKey);
      await api.confirmDecision(proposed[0].id);
      await waitFor('decision.confirmed', () => state().decisions[proposed[0].id]?.state === 'in_force');

      // -- Needs you: server order, one primary action, agent provenance -----------------------------
      const needs = (await api.inbox(CREW, 'project')).items as InboxItem[];
      expect(needs.map((i) => i.id)).toEqual(sortItems(needs).map((i) => i.id));
      expect(needs.some((i) => i.kind === 'decision_to_confirm')).toBe(false); // resolved by the confirm
      const ask = needs.find((i) => i.kind === 'human_question')!;
      expect(ask).toBeTruthy();
      const sessions = Object.values(state().sessions);
      expect(originLabel(ask, sessions)).toBe('from codex-1 (key-verified)');
      const action = primaryAction({ ...ask, project_id: 'yaadbooks' }, 'yaadbooks');
      expect(action).toMatchObject({ type: 'answer', messageId: question.id });
      // an agent cannot clear a human alarm; the human answers and resolves it
      const agentResolve = await keyApi.inboxItem(ask.id, 'resolve').catch((e: unknown) => e);
      expect(agentResolve).toMatchObject({ status: expect.any(Number) });
      expect([401, 403]).toContain((agentResolve as CrewApiError).status);
      await api.postMessage(CREW, { kind: 'answer', body: 'Yes, per line.', reply_to_id: ask.ref_id ?? undefined, clientMsgId: newClientMsgId() });
      await api.inboxItem(ask.id, 'resolve');
      const after = (await api.inbox(CREW, 'project')).items as InboxItem[];
      expect(after.some((i) => i.id === ask.id)).toBe(false);
      const overview = await api.inboxOverview();
      expect(Array.isArray(overview.items)).toBe(true);

      // -- Crew tab: first taker wins ----------------------------------------------------------------
      const crewItems = (await api.inbox(CREW, 'crew')).items as InboxItem[];
      expect(crewItems).toHaveLength(2);
      const work = crewItems.find((i) => i.state === 'open')!;
      const agents = crewItems.find((i) => i.state === 'claimed')!;
      expect(agents.claimed_by).toBe('cs_b'); // shown as "taken by codex-1"
      expect(primaryAction(work, 'yaadbooks')).toEqual({ type: 'claim', label: 'Take it' });
      const taken = await api.inboxItem(work.id, 'claim');
      expect((taken.item as InboxItem).state).toBe('claimed');
      const repeat = await api.inboxItem(work.id, 'claim'); // a repeat by the same taker is a no-op
      expect((repeat.item as InboxItem).claimed_by).toBe((taken.item as InboxItem).claimed_by);
      const lost = await api.inboxItem(agents.id, 'claim').catch((e: unknown) => e);
      expect((lost as CrewApiError).status).toBe(409);
      expect((lost as CrewApiError).code).toBe('already_claimed');
      report.second_claim_error = actionError(lost);

      // -- /freeze from the composer ------------------------------------------------------------------
      const freeze = parseComposer('/freeze pos Mani is editing POS himself', Object.values(state().zones), 'chat');
      expect(freeze.type).toBe('freeze');
      if (freeze.type === 'freeze') await api.freezeZone(freeze.zone.id, freeze.reason);
      await waitFor('zone frozen', () => !!state().zones.zn_pos.frozen_by);
      expect(parseComposer('/freeze pos again', Object.values(state().zones), 'chat')).toMatchObject({ type: 'invalid' });

      // -- session queues (details) folded from the real event log ------------------------------------
      const events: CrewEvent[] = [];
      let since = 0;
      for (;;) {
        const page = (await api.events(CREW, since, { limit: 200 })).data!;
        events.push(...page.events);
        if (!page.has_more || !page.events.length) break;
        since = page.events[page.events.length - 1].seq;
      }
      const queues = foldSessionQueues(events);
      expect((queues.get('cs_a') ?? []).some((i) => i.kind === 'mention')).toBe(true);
      expect((queues.get('cs_b') ?? []).some((i) => i.kind === 'mention')).toBe(true);
      report.queue_items = Object.fromEntries([...queues].map(([sid, items]) => [sid, items.length]));

      // -- notifications: the bell's list and read cursors ----------------------------------------
      const notes = (await api.notifications({ limit: 30 })) as unknown as NotificationList;
      expect(notes.items.some((n) => n.kind === 'decision' && !n.read)).toBe(true);
      expect(notes.unread).toBeGreaterThan(0);
      const upto = unreadUpto(notes.items);
      expect(Object.keys(upto)).toEqual([CREW]);
      await api.markNotificationsRead({ crew_id: CREW, upto_seq: upto[CREW] });
      const read = (await api.notifications({ limit: 30 })) as unknown as NotificationList;
      expect(read.unread).toBe(0);
      expect(read.items.every((n) => n.read)).toBe(true);
      report.notifications = notes.items.map((n) => n.kind);

      // -- real-time targets: human only; the webhook answers a signed challenge first ------------------
      const url = 'https://bridge.example.com/remembra';
      expect(targetProblem('webhook', url)).toBeNull();
      const refused = await keyApi.addNotifyTarget('webhook', url).catch((e: unknown) => e);
      expect((refused as CrewApiError).status).toBe(403);
      const hook = (await api.addNotifyTarget('webhook', url)) as unknown as AddedTarget;
      expect(hook.signing_secret).toMatch(/.{20,}/);
      expect(hook.verified_at).toBeTruthy();
      expect(hook.crews_without_channel).toEqual([CREW]); // default realtime is email only
      const detail = await api.getCrew(CREW);
      const realtime = ((detail.settings.notify as { realtime?: string[] }).realtime ?? []).slice();
      const patched = await api.patchSettings(CREW, { notify: { realtime: [...new Set([...realtime, 'webhook'])] } }, detail.settings_version);
      expect((patched.settings.notify as { realtime: string[] }).realtime).toEqual(['email', 'webhook']);
      expect(targetProblem('email', 'mani@example.com')).toBeNull();
      await api.addNotifyTarget('email', 'mani@example.com');
      const rules = (await api.notificationRules()) as unknown as NotificationRules;
      expect(rules.targets.map((t) => t.kind).sort()).toEqual(['email', 'webhook']);
      expect(JSON.stringify(rules)).not.toContain(hook.signing_secret!); // the secret is shown once, never listed

      // -- the reduced state still equals a fresh snapshot after all of it ---------------------------
      const fresh = (await api.snapshot(CREW)).data!;
      await waitFor('caught up', () => state().last_seq >= fresh.as_of_seq);
      expect(state().decisions).toEqual(fromSnapshot(fresh).decisions);
      expect(state().inbox_counts).toEqual(fromSnapshot(fresh).inbox_counts);

      report.last_seq = state().last_seq;
      report.signing_secret = hook.signing_secret;
      release();
    } finally {
      runtime.dispose();
      if (OUT) writeFileSync(OUT, JSON.stringify(report));
    }
  }, 60000);
});
