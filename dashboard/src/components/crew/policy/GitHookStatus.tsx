// Git-hook status per checkout (spec §8.4, §9.1 Policy): whether each live
// checkout still runs the commit and push gates. Husky or lefthook reinstalls
// can wipe them; crewd checks at every SessionStart and heartbeat and reports
// `githook_state`. A missing gate shows the exact command that reinstalls it.

import { beforeWriteLabel } from '../../../lib/crew/selectors';
import { CopyCommand } from '../../relay/ui';
import { PixelGlyph } from '../zones/PixelGlyph';
import { HOOK_FIX_COMMAND, HOOK_TEXT, type CheckoutRow } from './policyModel';

export function GitHookStatus({ rows }: { rows: CheckoutRow[] }) {
  if (!rows.length) {
    return <p className="text-sm text-ink-3">No agents are running, so no checkout has reported its git gates.</p>;
  }
  const missing = rows.filter((r) => r.hook === 'missing').length;
  const unknown = rows.filter((r) => r.hook === 'unknown').length;
  return (
    <div>
      <p className="text-sm text-ink-2">
        {missing
          ? `${missing} checkout${missing === 1 ? ' has' : 's have'} lost the commit gate: commits and pushes from there are not checked until it is back.`
          : unknown
            ? `${unknown} checkout${unknown === 1 ? ' has' : 's have'} not reported its git gates yet: commits and pushes there are checked only once the gates are installed.`
            : 'Every live checkout runs the commit and push gates.'}
      </p>
      <div className="mt-3 overflow-x-auto">
        <table className="cz-table">
          <thead>
            <tr>
              <th scope="col">Checkout</th>
              <th scope="col">Agents</th>
              <th scope="col">Git gates</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} data-live={r.hook === 'missing' ? 'true' : undefined}>
                <td className="font-mono">
                  <span className="block text-ink">{r.worktreeId ? `worktree ${r.worktreeId.slice(0, 12)}` : 'unknown checkout'}</span>
                  <span className="block text-[11px] text-ink-3">
                    {r.branches.length ? r.branches.join(', ') : 'branch not reported'}
                    {r.hostId ? ` · host ${r.hostId.slice(0, 10)}` : ''}
                  </span>
                </td>
                <td className="font-mono">{r.sessions.map((s) => `${s.callsign} (${beforeWriteLabel(s)})`).join(', ')}</td>
                <td>
                  <span className={`inline-flex items-center gap-1.5 font-mono ${r.hook === 'missing' ? 'text-fail' : 'text-ink'}`}>
                    <PixelGlyph name={r.hook === 'missing' ? 'breach' : r.hook === 'unknown' ? 'free' : 'held'} size={10} />
                    {HOOK_TEXT[r.hook]}
                  </span>
                  {r.hook === 'missing' && (
                    <>
                      <span className="mt-1 block text-[11px] text-ink-3">In that checkout (shows the change, asks before writing):</span>
                      <CopyCommand className="mt-1" command={HOOK_FIX_COMMAND} label="Reinstall the git gates" />
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
