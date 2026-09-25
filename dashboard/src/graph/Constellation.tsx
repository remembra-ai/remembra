// The Constellation: a live map of agents, projects, trail entries and the
// entities memory has resolved. Orange packets travel along the trail when
// a handoff lands or a message is sent; the status strip says what moved.

import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import clsx from 'clsx';
import { Maximize2, Minus, Pause, Play, Plus, Search, X } from 'lucide-react';
import { api } from '../lib/api';
import { relay, type InboxMessage, type TrailItem } from '../lib/relay';
import { navigate, useRoute } from '../lib/nav';
import { relativeTime } from '../lib/time';
import { useNow, useResource } from '../hooks/useResource';
import { useRelayData } from '../hooks/relayData';
import { useWebSocket, type WebSocketEvent } from '../hooks/useWebSocket';
import { watchReducedMotion } from '../brand/pixel';
import { agentMeta } from '../lib/agents';
import { ErrorNotice } from '../components/relay/ui';
import { ConstellationEngine } from './engine';
import { buildGraph, messageEvent, newEvents, trailEvent, type GNode, type GraphEvent } from './model';
import { NodeDrawer } from './NodeDrawer';

const WINDOWS = [
  { key: '24h', label: '24h', ms: 24 * 3600e3 },
  { key: '7d', label: '7d', ms: 7 * 24 * 3600e3 },
  { key: '14d', label: '14d', ms: 14 * 24 * 3600e3 },
  { key: 'all', label: 'all', ms: null },
] as const;

const STILL_KEY = 'remembra_constellation_still';

function readStill(): boolean | null {
  try {
    const v = window.localStorage.getItem(STILL_KEY);
    return v === null ? null : v === '1';
  } catch {
    return null;
  }
}

export function Constellation() {
  const { params } = useRoute();
  const project = params.get('project') || null;
  const agent = params.get('agent') || null;
  const windowKey = (WINDOWS.find((w) => w.key === params.get('window'))?.key ?? '7d') as (typeof WINDOWS)[number]['key'];
  const now = useNow(30000);
  const { summary } = useRelayData();

  const trail = useResource('constellation-trail', () => relay.trail({ limit: 100 }), { pollMs: 15000 });
  const inbox = useResource('constellation-inbox', () => relay.inboxMessages({ status: 'all', limit: 100 }), { pollMs: 20000 });
  const entities = useResource('constellation-entities', () => api.getEntityGraph('', 400, 3000), { pollMs: 120000 });

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<ConstellationEngine | null>(null);
  const [engineError, setEngineError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [reduced, setReduced] = useState(false);
  const [stillChoice, setStillChoice] = useState<boolean | null>(() => readStill());
  const still = stillChoice ?? reduced;
  const [status, setStatus] = useState<{ event: GraphEvent; live: boolean } | null>(null);
  const seen = useRef<Set<string> | null>(null);
  const searchId = useId();

  useEffect(() => watchReducedMotion(setReduced), []);

  // Engine lifecycle.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    let engine: ConstellationEngine;
    try {
      engine = new ConstellationEngine(canvas, {
        onSelect: (id) => setSelected(id),
        onEventStart: (event) => setStatus({ event, live: !!event.live }),
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'The graph could not start in this browser.';
      queueMicrotask(() => setEngineError(message));
      return undefined;
    }
    engineRef.current = engine;
    // Dev builds expose the engine for profiling from the console.
    if (import.meta.env.DEV) (canvas as HTMLCanvasElement & { __engine?: ConstellationEngine }).__engine = engine;
    return () => {
      engine.destroy();
      engineRef.current = null;
    };
  }, []);

  useEffect(() => {
    engineRef.current?.setStill(still);
  }, [still]);

  const sinceMs = useMemo(() => {
    const w = WINDOWS.find((x) => x.key === windowKey);
    return w && w.ms !== null ? now.getTime() - w.ms : null;
  }, [windowKey, now]);

  const trailItems: TrailItem[] = useMemo(() => trail.data?.items ?? [], [trail.data]);
  const messages: InboxMessage[] = useMemo(() => inbox.data?.items ?? [], [inbox.data]);

  const graph = useMemo(
    () => buildGraph({ summary: summary.data, trail: trailItems, inbox: messages, entities: entities.data }, { project, agent, sinceMs }),
    [summary.data, trailItems, messages, entities.data, project, agent, sinceMs],
  );

  useEffect(() => {
    engineRef.current?.setData(graph);
  }, [graph]);

  useEffect(() => {
    engineRef.current?.setSelected(selected);
  }, [selected]);

  // Events: replay the three most recent on first load (real ones, with their
  // age in the strip), then play whatever arrives while the page is open.
  useEffect(() => {
    if (!trail.data || !inbox.data) return;
    const events = [
      ...trailItems.map((item) => trailEvent(item, summary.data)).filter((e): e is GraphEvent => !!e),
      ...messages.map(messageEvent),
    ].filter((e) => (project ? e.path.includes(`p:${project}`) || e.kind === 'message' : true));
    if (seen.current === null) {
      seen.current = new Set(events.map((e) => e.key));
      const recent = [...events].sort((a, b) => b.at - a.at).slice(0, 3).reverse();
      for (const e of recent) engineRef.current?.play({ ...e, live: false });
      return;
    }
    const fresh = newEvents(seen.current, events);
    for (const e of fresh) {
      seen.current.add(e.key);
      engineRef.current?.play({ ...e, live: true });
    }
  }, [trail.data, inbox.data, trailItems, messages, summary.data, project]);

  // A memory stored anywhere (WebSocket): refresh the trail so a new handoff
  // or checkpoint arrives within a second instead of at the next poll.
  const refreshTrail = trail.refresh;
  const onMemoryEvent = useCallback(
    (event: WebSocketEvent) => {
      if (event.type === 'memory.created') refreshTrail();
    },
    [refreshTrail],
  );
  const { connected } = useWebSocket({
    baseUrl: api.getApiBaseUrl() || undefined,
    namespace: 'default',
    apiKey: api.getApiKey() || undefined,
    token: api.getJwtToken() || undefined,
    onMemoryEvent,
  });

  // Search: highlight matches in the graph; Enter or a click selects one.
  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    return graph.nodes
      .filter((n) => n.label.toLowerCase().includes(q) || (n.detail ?? '').toLowerCase().includes(q))
      .sort((a, b) => b.weight - a.weight)
      .slice(0, 8);
  }, [query, graph.nodes]);

  useEffect(() => {
    const q = query.trim().toLowerCase();
    if (!q) {
      engineRef.current?.setHighlight(null);
      return;
    }
    const ids = new Set(graph.nodes.filter((n) => n.label.toLowerCase().includes(q) || (n.detail ?? '').toLowerCase().includes(q)).map((n) => n.id));
    engineRef.current?.setHighlight(ids);
  }, [query, graph.nodes]);

  const pick = (node: GNode) => {
    setSelected(node.id);
    engineRef.current?.focus(node.id);
  };

  const setParam = (key: string, value: string | null) => {
    const next: Record<string, string | null> = { project, agent, window: windowKey === '7d' ? null : windowKey };
    next[key] = value;
    navigate('graph', next, true);
  };

  const toggleStill = () => {
    const next = !still;
    setStillChoice(next);
    try {
      window.localStorage.setItem(STILL_KEY, next ? '1' : '0');
    } catch {
      // Per-viewer convenience only.
    }
  };

  const selectedNode = selected ? (graph.nodes.find((n) => n.id === selected) ?? null) : null;
  const projects = useMemo(() => [...new Set((summary.data?.projects ?? []).map((p) => p.project_id || 'default'))].sort(), [summary.data]);
  const agents = useMemo(() => (summary.data?.agents ?? []).map((a) => a.agent_id).sort(), [summary.data]);
  const counts = useMemo(() => {
    const c = { agent: 0, project: 0, entity: 0, trail: 0 };
    for (const n of graph.nodes) {
      if (n.kind === 'agent') c.agent += 1;
      else if (n.kind === 'project') c.project += 1;
      else if (n.kind === 'entity') c.entity += 1;
      else c.trail += 1;
    }
    return c;
  }, [graph.nodes]);

  const loading = trail.loading || summary.loading;
  const loadError = !trail.data && trail.error != null ? trail.error : null;
  const empty = !loading && !loadError && graph.nodes.length === 0;

  const statusText = status
    ? `${status.live ? '' : `${relativeTime(new Date(status.event.at), now)} · `}${status.event.text}`
    : trail.data
      ? 'Quiet. Packets travel here the moment an agent hands off or sends a note.'
      : '';

  return (
    <div className="flex h-[calc(100dvh-13rem)] min-h-[460px] flex-col gap-3 md:h-[calc(100dvh-10rem)]">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[200px] flex-1 sm:max-w-xs">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" aria-hidden="true" />
          <label htmlFor={searchId} className="sr-only">
            Search the graph
          </label>
          <input
            id={searchId}
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && matches[0]) pick(matches[0]);
              if (e.key === 'Escape') setQuery('');
            }}
            placeholder="Search agents, projects, entities"
            className="rr-input w-full py-1.5 pl-8 pr-2 text-sm"
            aria-controls={`${searchId}-results`}
          />
          {matches.length > 0 && (
            <ul id={`${searchId}-results`} className="modal-surface absolute inset-x-0 top-full z-20 mt-1 max-h-72 overflow-y-auto rounded-[3px] py-1">
              {matches.map((n) => (
                <li key={n.id}>
                  <button
                    type="button"
                    onClick={() => pick(n)}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-ink hover:bg-signal-wash"
                  >
                    <span className="w-16 shrink-0 font-mono text-[10px] uppercase tracking-[0.06em] text-ink-3">{n.kind}</span>
                    <span className="truncate">{n.label}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <label className="sr-only" htmlFor={`${searchId}-project`}>
          Project
        </label>
        <select
          id={`${searchId}-project`}
          value={project ?? ''}
          onChange={(e) => setParam('project', e.target.value || null)}
          className="rr-input py-1.5 pl-2 pr-7 text-sm"
        >
          <option value="">All projects</option>
          {projects.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <label className="sr-only" htmlFor={`${searchId}-agent`}>
          Agent
        </label>
        <select
          id={`${searchId}-agent`}
          value={agent ?? ''}
          onChange={(e) => setParam('agent', e.target.value || null)}
          className="rr-input py-1.5 pl-2 pr-7 text-sm"
        >
          <option value="">All agents</option>
          {agents.map((a) => (
            <option key={a} value={a}>
              {agentMeta(a).name}
            </option>
          ))}
        </select>
        <div role="radiogroup" aria-label="Time window" className="inline-flex border border-rule">
          {WINDOWS.map((w) => (
            <button
              key={w.key}
              type="button"
              role="radio"
              aria-checked={windowKey === w.key}
              onClick={() => setParam('window', w.key === '7d' ? null : w.key)}
              className={clsx('px-2.5 py-1.5 font-mono text-xs', windowKey === w.key ? 'bg-ink text-paper' : 'text-ink-2 hover:text-ink')}
            >
              {w.label}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-1">
          <button type="button" onClick={toggleStill} className="rr-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs" aria-pressed={still}>
            {still ? <Play className="h-3.5 w-3.5" aria-hidden="true" /> : <Pause className="h-3.5 w-3.5" aria-hidden="true" />}
            {still ? 'Motion' : 'Still'}
          </button>
          <button type="button" onClick={() => engineRef.current?.zoomBy(0.8)} className="rr-btn-ghost p-1.5" aria-label="Zoom out">
            <Minus className="h-4 w-4" />
          </button>
          <button type="button" onClick={() => engineRef.current?.zoomBy(1.25)} className="rr-btn-ghost p-1.5" aria-label="Zoom in">
            <Plus className="h-4 w-4" />
          </button>
          <button type="button" onClick={() => engineRef.current?.fit()} className="rr-btn-ghost p-1.5" aria-label="Fit the graph">
            <Maximize2 className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Stage */}
      <div className="relative min-h-0 flex-1 overflow-hidden rounded-[3px] border border-rule bg-paper">
        <canvas
          ref={canvasRef}
          tabIndex={0}
          className="absolute inset-0 block h-full w-full outline-none focus-visible:outline-2 focus-visible:outline-signal"
          aria-label="Constellation graph of your agents, projects, trail entries and entities. Arrow keys pan, plus and minus zoom, 0 fits, Escape clears the selection. Use search to jump to a node."
          aria-describedby={`${searchId}-legend`}
        />

        {engineError && <p className="absolute inset-x-4 top-4 text-sm text-fail">{engineError}</p>}
        {loadError != null && (
          <div className="absolute inset-x-4 top-4 rr-card rounded-[3px]">
            <ErrorNotice error={loadError} what="the trail" onRetry={trail.refresh} compact />
          </div>
        )}
        {empty && (
          <div className="absolute inset-0 flex items-center justify-center p-6">
            <div className="rr-win max-w-sm">
              <p className="rr-win-bar">
                <i aria-hidden="true" />
                constellation.empty
              </p>
              <p className="px-3 py-3 text-ink-2">
                {project || agent || windowKey !== 'all'
                  ? 'Nothing matches these filters. Widen the time window or clear the project and agent.'
                  : 'No agents, projects or entities yet. Connect an agent from Home; its first handoff lights up here.'}
              </p>
            </div>
          </div>
        )}

        {/* Legend */}
        <ul
          id={`${searchId}-legend`}
          className="pointer-events-none absolute left-3 top-3 hidden space-y-1 bg-paper/80 px-2 py-1.5 font-mono text-[10px] text-ink-3 sm:block"
        >
          <li className="flex items-center gap-1.5">
            <span aria-hidden="true" className="inline-block h-2.5 w-2.5 rounded-full bg-ink-3" /> agent
          </li>
          <li className="flex items-center gap-1.5">
            <span aria-hidden="true" className="inline-block h-2.5 w-2.5 border-2 border-ink" /> project
          </li>
          <li className="flex items-center gap-1.5">
            <span aria-hidden="true" className="inline-block h-1.5 w-3 rounded-full bg-signal" /> handoff
          </li>
          <li className="flex items-center gap-1.5">
            <span aria-hidden="true" className="inline-block h-2 w-2 rotate-45 bg-ink-3" /> entity
          </li>
        </ul>

        {/* Status strip */}
        <div className="pointer-events-none absolute inset-x-3 bottom-3 flex justify-center">
          <p
            className="flex max-w-full items-center gap-2.5 rounded-[3px] border border-rule-strong bg-panel px-3 py-2 font-mono text-[12px] text-ink-2 shadow-[var(--shadow)]"
            aria-live="polite"
          >
            <span aria-hidden="true" className={clsx('h-2 w-2 shrink-0 rounded-full', status?.live ? 'rr-pulse bg-signal' : 'bg-signal')} />
            <span className="truncate">{statusText || 'Loading the trail…'}</span>
          </p>
        </div>

        {selectedNode && (
          <NodeDrawer
            node={selectedNode}
            trail={trailItems}
            messages={messages}
            now={now}
            onClose={() => {
              setSelected(null);
              canvasRef.current?.focus();
            }}
          />
        )}
      </div>

      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-ink-3">
        <span>
          {counts.agent} agents · {counts.project} projects · {counts.trail} trail entries · {counts.entity} entities
        </span>
        <span aria-hidden="true">·</span>
        <span>{connected ? 'live (socket + polling)' : 'live (polling every 15s)'}</span>
        {entities.data?.stats && (entities.data.stats as { truncated_nodes?: boolean }).truncated_nodes && (
          <>
            <span aria-hidden="true">·</span>
            <span>showing the 400 most-referenced entities</span>
          </>
        )}
        {windowKey !== 'all' && (
          <>
            <span aria-hidden="true">·</span>
            <span>entities are not time-stamped, so the window filters trail and messages</span>
          </>
        )}
        {query && matches.length === 0 && (
          <button type="button" onClick={() => setQuery('')} className="inline-flex items-center gap-1 text-ink-2 underline">
            no matches <X className="h-3 w-3" aria-hidden="true" />
          </button>
        )}
      </p>
    </div>
  );
}
