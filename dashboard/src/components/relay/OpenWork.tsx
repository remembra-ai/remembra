import { useEffect, useRef, useState } from 'react';
import { api, ApiError, type Memory } from '../../lib/api';
import { continuity, loadOpenWork, type OpenWorkItem } from '../../lib/continuity';
import { hrefFor } from '../../lib/nav';
import { trustNotice } from '../../lib/handoffTrust';
import { useResource } from '../../hooks/useResource';
import { Card, CardHeader, ErrorNotice, StaleNotice } from './ui';

const BUTTON = 'rounded-[2px] border border-rule px-3 py-1.5 font-mono text-xs text-ink hover:border-ink disabled:opacity-50';

export function WorkReport({ item, projectId }: { item: OpenWorkItem; projectId: string }) {
  const notice = trustNotice(typeof item.trust_score === 'number' ? {
    trust_score: item.trust_score, withheld: item.withheld === true, flags: item.flags ?? [],
  } : undefined);
  return (
    <>
      <p className="font-mono text-xs text-ink-3">
        {item.kind === 'failure' ? 'Reported failure' : 'Outstanding task'} · {item.state === 'resolution_proposed' ? 'resolution awaiting review' : 'open'}
      </p>
      <p className="mt-2 whitespace-pre-wrap break-words text-sm text-ink">{item.text}</p>
      <p className="mt-2 text-xs text-ink-3">Reported by an agent; verify against the source and test evidence.</p>
      {notice && <p className="mt-2 text-xs text-ink-2">{notice.text}</p>}
      <a className="mt-2 inline-block font-mono text-xs text-signal underline" href={hrefFor('trail', { project: projectId, open: item.source_handoff_id })}>
        View source handoff
      </a>
    </>
  );
}

function ResolutionReview({ item, projectId, onResolved, onChanged, disabled }: {
  item: OpenWorkItem;
  projectId: string;
  onResolved: () => void;
  onChanged: () => void;
  disabled: boolean;
}) {
  const evidence = useResource(`resolution:${projectId}:${item.id}:${item.version}:${item.resolution_digest}`, async () => {
    if (!item.resolution_memory_id) throw new Error('This proposal has no evidence reference.');
    const memory = await api.request<Memory>(`/memories/${encodeURIComponent(item.resolution_memory_id)}`);
    if (memory.project_id !== projectId || memory.id !== item.resolution_memory_id) {
      throw new Error('The evidence does not belong to this project or proposal.');
    }
    return memory;
  });
  const [reviewed, setReviewed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const human = api.getAuthMode() === 'jwt';

  const confirm = async () => {
    if (!human || !reviewed || !evidence.data || saving || changed || evidence.error || disabled) return;
    setSaving(true);
    setError(null);
    try {
      const result = await continuity.confirm(projectId, item);
      if (!active.current) return;
      if (result.id !== item.id || result.state !== 'resolved' || result.version !== item.version + 1) {
        throw new Error('The server did not confirm this resolution. Refresh before trying again.');
      }
      setChanged(true);
      onResolved();
    } catch (err) {
      if (!active.current) return;
      setReviewed(false);
      if (err instanceof ApiError && err.status === 409) {
        setChanged(true);
        setError('The report or evidence changed. Refresh and review again. Changed evidence requires a new resolution proposal.');
        onChanged();
      } else {
        setError(err instanceof ApiError && err.status === 403
          ? 'Your current login cannot accept this resolution. Sign in as the account holder.'
          : err instanceof Error ? err.message : 'The resolution could not be accepted.');
      }
    } finally {
      if (active.current) setSaving(false);
    }
  };

  return (
    <div className="mt-4 border-t border-rule pt-3">
      <h4 className="font-display font-bold text-ink">Proposed resolution evidence</h4>
      {!item.evidence_available && <p className="mt-2 text-sm text-ink-2">The original report is unavailable. Obtain a current report before accepting a resolution.</p>}
      {evidence.loading && <p className="mt-2 text-sm text-ink-3">Loading evidence…</p>}
      {evidence.error != null && <ErrorNotice error={evidence.error} what="resolution evidence" onRetry={() => { setReviewed(false); evidence.refresh(); }} />}
      {evidence.data && (
        <>
          <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-[2px] border border-rule bg-paper px-3 py-3 font-mono text-xs text-ink">{evidence.data.content}</pre>
          <p className="mt-2 text-xs text-ink-3">This is recorded evidence. Acceptance records your review; check the claimed commit, tests and deployment yourself.</p>
          {human ? (
            <label className="mt-3 flex items-start gap-2 text-sm text-ink">
              <input type="checkbox" checked={reviewed} disabled={saving || changed || disabled || evidence.error != null} onChange={(event) => setReviewed(event.target.checked)} />
              I reviewed this evidence and accept that this item is resolved.
            </label>
          ) : <p className="mt-3 text-sm text-ink-2">Sign in as the account holder to accept a resolution. An API key cannot approve it.</p>}
          <button type="button" className={`${BUTTON} mt-3`} disabled={!human || !reviewed || saving || changed || disabled || evidence.error != null} onClick={() => void confirm()}>
            {saving ? 'Accepting…' : 'Accept resolution'}
          </button>
        </>
      )}
      {error && <p role="alert" className="mt-3 text-sm text-ink">{error}</p>}
    </div>
  );
}

function WorkCard({ item, projectId, stale, onResolved, onChanged }: {
  item: OpenWorkItem; projectId: string; stale: boolean; onResolved: () => void; onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <article className="rounded-[2px] border border-rule p-3">
      <WorkReport item={item} projectId={projectId} />
      {item.state === 'resolution_proposed' && (
        <details className="mt-3" onToggle={(event) => setOpen(event.currentTarget.open)}>
          <summary className="cursor-pointer font-mono text-xs text-ink">Review proposed resolution</summary>
          {open && <ResolutionReview key={`${item.id}:${item.version}:${item.resolution_digest}`} item={item} projectId={projectId}
            disabled={stale || !item.evidence_available} onResolved={onResolved} onChanged={onChanged} />}
        </details>
      )}
    </article>
  );
}

/** Mounted with a project key: switching projects clears pages and evidence review. */
export function OpenWork({ projectId }: { projectId: string }) {
  const [pages, setPages] = useState(1);
  const [message, setMessage] = useState<string | null>(null);
  const work = useResource(`open-work:${projectId}:${pages}`, () => loadOpenWork(projectId, pages), { pollMs: 30000 });
  return (
    <Card labelledBy="open-work-heading">
      <CardHeader id="open-work-heading" title="Unresolved work" eyebrow={projectId} action={
        <button type="button" className={BUTTON} disabled={work.refreshing} onClick={work.refresh}>Refresh unresolved work</button>
      } />
      <div className="px-4 pb-4 pt-2 sm:px-5">
        <p className="text-sm text-ink-2">Failures and tasks survive newer handoffs. A proposed fix stays open until you review and accept it.</p>
        <p className="mt-1 text-xs text-ink-3">Includes reports from your connected agents for this project.</p>
        {message && <p role="status" className="mt-3 text-sm text-ink">{message}</p>}
        {work.loading && <p className="mt-4 text-sm text-ink-3">Loading unresolved work…</p>}
        {!work.data && work.error != null && <ErrorNotice error={work.error} what="unresolved work" onRetry={work.refresh} />}
        {work.data && work.error != null && <StaleNotice error={work.error} what="unresolved work" />}
        {work.data && (
          <>
            <p className="mt-4 font-mono text-xs text-ink-3" aria-live="polite">{work.data.total} unresolved · {work.data.items.length} shown</p>
            {work.data.total === 0 && <p className="mt-3 text-sm text-ink">No unresolved tasks or failures are recorded for this project.</p>}
            <div className="mt-3 space-y-3">
              {work.data.items.map((item) => (
                <WorkCard key={item.id} item={item} projectId={projectId} stale={work.error != null || work.refreshing}
                  onResolved={() => { setMessage('Resolution accepted.'); work.refresh(); }}
                  onChanged={work.refresh} />
              ))}
            </div>
            {work.data.next_after && <button type="button" className={`${BUTTON} mt-4`} disabled={work.refreshing} onClick={() => setPages((n) => n + 1)}>Load more unresolved work</button>}
          </>
        )}
      </div>
    </Card>
  );
}
