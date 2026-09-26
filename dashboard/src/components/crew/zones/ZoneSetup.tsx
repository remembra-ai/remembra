// Setup mode (spec §9.5, §9.14, D37). After the no-zone bootstrap the
// temporary zones are listed as "temporary zones (auto)" with Keep / Rename /
// Merge / Split / Drop, and "Save as .remembra/zones.yml" gives the file and a
// patch to commit. "Undo" removes the temporary zones (human-only).
// For a crew with no zones at all it starts from the server's suggestions.

import { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { Check, Copy } from 'lucide-react';
import { useCopy } from '../../../hooks/useCopy';
import type { HumanActionRunner } from '../policy/useHumanAction';
import { actionErrorText } from '../policy/stepUp';
import { PixelGlyph } from './PixelGlyph';
import { ZoneActionConfirm } from './ZoneActionConfirm';
import { undoTemporaryZones } from './zoneActions';
import {
  dropZone,
  initialPlan,
  mergeZones,
  newFilePatch,
  planErrors,
  planFromSuggestions,
  planToYaml,
  renameZone,
  splitBlocker,
  splitZone,
  ZONES_PATH,
  type SetupPlan,
} from './setupPlan';
import type { ZonesApi } from './zonesApi';
import type { RepoTreeNode, ZoneDetail } from './zoneModel';

const STORE_KEY = (crewId: string) => `remembra.crew.zoneSetup.${crewId}`;

function loadDraft(crewId: string): SetupPlan | null {
  try {
    const raw = localStorage.getItem(STORE_KEY(crewId));
    const plan = raw ? (JSON.parse(raw) as SetupPlan) : null;
    return plan && Array.isArray(plan.zones) && Array.isArray(plan.log) ? plan : null;
  } catch {
    return null;
  }
}

function saveDraft(crewId: string, plan: SetupPlan | null): void {
  try {
    if (plan) localStorage.setItem(STORE_KEY(crewId), JSON.stringify(plan));
    else localStorage.removeItem(STORE_KEY(crewId));
  } catch {
    /* private window or blocked storage: the draft just is not remembered */
  }
}

function CopyButton({ text, label, toastText }: { text: string; label: string; toastText: string }) {
  const [copy, copied] = useCopy();
  return (
    <button type="button" className="rr-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs" onClick={() => copy(text, toastText)}>
      {copied ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
      {label}
    </button>
  );
}

function RenameRow({ slug, title, onSave, onCancel }: { slug: string; title: string; onSave: (slug: string, title: string) => void; onCancel: () => void }) {
  const [s, setS] = useState(slug);
  const [t, setT] = useState(title);
  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        onSave(s, t);
      }}
    >
      <label className="text-xs">
        <span className="block font-mono text-[10.5px] text-ink-3">slug</span>
        <input className="rr-input w-36 px-2 py-1 font-mono text-[12px]" value={s} onChange={(e) => setS(e.target.value)} autoFocus />
      </label>
      <label className="text-xs">
        <span className="block font-mono text-[10.5px] text-ink-3">title</span>
        <input className="rr-input w-52 px-2 py-1 text-[13px]" value={t} maxLength={120} onChange={(e) => setT(e.target.value)} />
      </label>
      <button type="submit" className="rr-btn-primary px-2.5 py-1 text-xs">
        Rename
      </button>
      <button type="button" className="rr-btn-ghost px-2.5 py-1 text-xs" onClick={onCancel}>
        Cancel
      </button>
    </form>
  );
}

export function ZoneSetup({
  crewId,
  zones,
  tree,
  bootstrap,
  canAct,
  why,
  api,
  runner,
  onChanged,
}: {
  crewId: string;
  zones: readonly ZoneDetail[];
  tree: RepoTreeNode | null;
  /** True: temporary zones are live (bootstrap). False: no zones at all; draft from suggestions. */
  bootstrap: boolean;
  canAct: boolean;
  why: string | null;
  api: ZonesApi;
  runner: HumanActionRunner;
  onChanged: () => void;
}) {
  const [plan, setPlan] = useState<SetupPlan | null>(() => loadDraft(crewId) ?? (bootstrap ? initialPlan(zones) : null));
  const [renaming, setRenaming] = useState<string | null>(null);
  const [undoing, setUndoing] = useState(false);
  const [suggestError, setSuggestError] = useState<string | null>(null);
  const [showFile, setShowFile] = useState(false);

  useEffect(() => saveDraft(crewId, plan), [crewId, plan]);

  const yaml = useMemo(() => (plan ? planToYaml(plan) : ''), [plan]);
  const errors = useMemo(() => (plan ? planErrors(plan) : []), [plan]);
  const temporary = zones.filter((z) => z.source === 'suggested' && !z.archived_at);

  const draftFromSuggestions = async () => {
    setSuggestError(null);
    try {
      const res = await api.suggestZones(crewId);
      if (!res.zones.length) {
        setSuggestError(res.reason === 'no_tree' ? 'No folder tree has been uploaded yet: start an agent in the repo first.' : 'No feature folders found to suggest.');
        return;
      }
      setPlan(planFromSuggestions(res.zones));
    } catch (err) {
      setSuggestError(actionErrorText(err));
    }
  };

  if (!plan) {
    return (
      <section className="rounded-[3px] border border-rule bg-panel p-4 sm:p-5" aria-label="No zones yet">
        <p className="rr-eyebrow">No zones yet</p>
        <p className="mt-2 max-w-[42em] text-sm text-ink-2">
          Zones tell agents what not to touch. They come from <span className="font-mono">.remembra/zones.yml</span> in the repository, or automatically from your
          folders the moment a second agent joins.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button type="button" className="rr-btn-primary px-3 py-1.5 text-sm" onClick={() => void draftFromSuggestions()}>
            Draft zones from my folders
          </button>
          {suggestError && <span className="text-sm text-fail">{suggestError}</span>}
        </div>
      </section>
    );
  }

  const others = (key: string) => plan.zones.filter((z) => z.key !== key);

  return (
    <section className="rounded-[3px] border border-rule-strong bg-panel" aria-labelledby="zone-setup-title">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-dashed border-rule px-4 py-3 sm:px-5">
        <div>
          <p className="rr-eyebrow">{bootstrap ? 'Temporary zones (auto)' : 'Draft zones'}</p>
          <h2 id="zone-setup-title" className="font-display mt-1 text-lg font-bold text-ink">
            {bootstrap ? 'Made from your folders when a second agent joined' : 'Shape your zones, then save the file'}
          </h2>
          <p className="mt-1 max-w-[46em] text-sm text-ink-2">
            {bootstrap
              ? 'They already keep agents apart (enforced). Keep, rename, merge, split or drop them, then save them as the repository file so they stay.'
              : 'Nothing is enforced until the file is committed: commit it on your default branch and crewd uploads it.'}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="rr-btn-ghost px-2.5 py-1.5 text-xs" onClick={() => setPlan(bootstrap ? initialPlan(zones) : null)}>
            Start over
          </button>
          {bootstrap && temporary.length > 0 && canAct && (
            <button type="button" className="rr-btn-ghost px-2.5 py-1.5 text-xs" onClick={() => setUndoing(true)}>
              Undo temporary zones
            </button>
          )}
        </div>
      </div>

      <ol className="divide-y divide-dashed divide-rule">
        {plan.zones.map((z) => {
          const blocker = splitBlocker(z, tree);
          return (
            <li key={z.key} className="px-4 py-3 sm:px-5">
              {renaming === z.key ? (
                <RenameRow
                  slug={z.slug}
                  title={z.title}
                  onCancel={() => setRenaming(null)}
                  onSave={(slug, title) => {
                    setPlan(renameZone(plan, z.key, slug, title));
                    setRenaming(null);
                  }}
                />
              ) : (
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <div className="min-w-0">
                    <span className="flex flex-wrap items-baseline gap-2">
                      <PixelGlyph name="held" size={10} />
                      <span className="cz-chip" data-mode="exclusive">
                        {z.slug}
                      </span>
                      <span className="text-sm text-ink-2">{z.title}</span>
                    </span>
                    <span className="mt-1 flex flex-wrap gap-1">
                      {z.include.map((g) => (
                        <code key={g} className="cz-glob">
                          {g}
                        </code>
                      ))}
                    </span>
                    {(z.from.length > 1 || z.from[0] !== z.slug) && <span className="mt-1 block font-mono text-[10.5px] text-ink-3">from {z.from.join(' + ')}</span>}
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="font-mono text-[10.5px] uppercase tracking-[0.06em] text-ink-3">keep</span>
                    <button type="button" className="rr-btn-ghost px-2 py-1 font-mono text-[11px]" onClick={() => setRenaming(z.key)}>
                      Rename
                    </button>
                    {others(z.key).length > 0 && (
                      <label className="inline-flex items-center">
                        <span className="sr-only">Merge {z.slug} into</span>
                        <select
                          className="rr-input px-1.5 py-1 font-mono text-[11px]"
                          value=""
                          onChange={(e) => e.target.value && setPlan(mergeZones(plan, z.key, e.target.value))}
                        >
                          <option value="">Merge into…</option>
                          {others(z.key).map((o) => (
                            <option key={o.key} value={o.key}>
                              {o.slug}
                            </option>
                          ))}
                        </select>
                      </label>
                    )}
                    <button
                      type="button"
                      className="rr-btn-ghost px-2 py-1 font-mono text-[11px] disabled:opacity-40"
                      disabled={blocker !== null}
                      title={blocker ?? 'One zone per subfolder'}
                      onClick={() => setPlan(splitZone(plan, z.key, tree))}
                    >
                      Split
                    </button>
                    <button type="button" className="rr-btn-ghost px-2 py-1 font-mono text-[11px]" onClick={() => setPlan(dropZone(plan, z.key))}>
                      Drop
                    </button>
                  </div>
                </div>
              )}
            </li>
          );
        })}
        {!plan.zones.length && <li className="px-4 py-3 text-sm text-ink-3 sm:px-5">Every zone was dropped. Start over to get them back.</li>}
      </ol>

      {plan.log.length > 0 && (
        <p className="border-t border-dashed border-rule px-4 py-2 font-mono text-[11px] text-ink-3 sm:px-5">{plan.log.slice(-4).join(' · ')}</p>
      )}

      <div className="border-t border-rule px-4 py-3 sm:px-5">
        {errors.length > 0 ? (
          <ul className="space-y-0.5 text-sm text-fail" role="alert">
            {errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className="rr-btn-primary px-3 py-1.5 text-sm" onClick={() => setShowFile((v) => !v)} aria-expanded={showFile}>
              Save as {ZONES_PATH}
            </button>
            <span className="text-xs text-ink-3">Commit it on the default branch; removing a live zone waits for your approval here.</span>
          </div>
        )}
        {showFile && errors.length === 0 && (
          <div className="mt-3">
            <div className="flex flex-wrap gap-2">
              <CopyButton text={yaml} label="Copy zones.yml" toastText="zones.yml copied" />
              <CopyButton text={newFilePatch(yaml)} label="Copy as patch (git apply)" toastText="Patch copied" />
            </div>
            <pre className="rr-cmd mt-2 max-h-80 overflow-auto rounded-[3px] p-3 font-mono text-[12px] leading-relaxed whitespace-pre">{yaml}</pre>
          </div>
        )}
      </div>

      {undoing && (
        <ZoneActionConfirm
          spec={{
            title: 'Undo the temporary zones',
            consequence: `${temporary.length} temporary zone${temporary.length === 1 ? '' : 's'} are removed and their claims released. Agents are then kept apart only by same-file checks until you add zones.`,
            warning: 'Agents already working in these folders lose their protection from each other.',
            confirmLabel: 'Undo zones',
            danger: true,
            reason: false,
          }}
          onConfirm={async () => {
            if (!canAct) throw new Error(why ?? 'Only a dashboard login can do this.');
            await undoTemporaryZones(zones, api, runner.run);
            saveDraft(crewId, null);
            setPlan(null);
            toast.success('Temporary zones removed');
            onChanged();
          }}
          onClose={() => setUndoing(false)}
        />
      )}
    </section>
  );
}
