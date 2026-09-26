// The notifications bell in the header (§9.11): a hand-set pixel bell with an
// orange count block, opening the Notification Center. Real-time kinds say
// they also went out by email or webhook; the footer leads to the targets.

import { useEffect, useId, useRef, useState } from 'react';
import clsx from 'clsx';
import { navigate } from '../../../lib/nav';
import { absoluteTime, relativeTime } from '../../../lib/time';
import { useNow } from '../../../hooks/useResource';
import { PixelGlyph } from '../channel/pixels';
import type { GlyphName } from '../channel/dither';
import { badgeText, kindTitle, notificationHref, unreadUpto, type NotificationItem } from './model';
import { useNotifications, type Notifications } from './useNotifications';

const GLYPH: Record<string, GlyphName> = {
  handoff: 'baton',
  collision: 'collision',
  tamper: 'shield',
  bypass: 'shield',
  githook: 'hook',
  zone_change: 'zone',
  decision: 'decision',
  stuck: 'clock',
  checkpoint_missed: 'clock',
  task_done: 'check',
};

export function NotificationCenter({
  data,
  onClose,
  titleId,
}: {
  data: Notifications;
  onClose: () => void;
  titleId: string;
}) {
  const now = useNow(30000);
  const items = data.list?.items ?? [];
  const unread = data.list?.unread ?? 0;
  const open = (item: NotificationItem) => {
    void data.markRead(item.crew_id, item.seq).catch(() => undefined);
    onClose();
  };
  return (
    <div className="crew-px flex max-h-[min(70vh,560px)] flex-col">
      <div className="flex items-center gap-2 border-b border-rule px-4 py-3">
        <h2 id={titleId} className="font-display text-base font-bold text-ink">
          Notifications
        </h2>
        {unread > 0 && <span className="bg-signal px-1.5 font-mono text-[11px] leading-5 text-on-signal">{unread} new</span>}
        <button
          type="button"
          disabled={unread === 0}
          onClick={() => void data.markRead(null).catch(() => undefined)}
          className="ml-auto font-mono text-[11px] text-ink-2 underline decoration-rule underline-offset-2 hover:text-ink disabled:no-underline disabled:opacity-40"
        >
          Mark all read
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {data.loading && <p className="px-4 py-6 font-mono text-[12px] text-ink-3">Loading…</p>}
        {!data.loading && data.error != null && !data.list && (
          <p className="px-4 py-6 text-sm text-ink-2">Notifications are not available on this server yet (crew mode is off or the API is older).</p>
        )}
        {data.list && items.length === 0 && (
          <div className="px-4 py-8 text-center">
            <PixelGlyph name="bell" size={28} className="mx-auto text-ink-3" />
            <p className="mt-3 text-sm text-ink-2">Nothing yet. Handoffs, collisions, tamper blocks and decisions to confirm land here.</p>
          </div>
        )}
        {items.length > 0 && (
          <ul className="divide-y divide-rule">
            {items.map((item) => (
              <li key={`${item.crew_id}:${item.seq}`}>
                <a href={notificationHref(item)} onClick={() => open(item)} className={clsx('flex w-full gap-3 px-4 py-3 text-left hover:bg-paper-2', !item.read && 'bg-signal-wash/40')}>
                  <span className="mt-0.5 text-ink-2">
                    <PixelGlyph name={GLYPH[item.kind] ?? 'bell'} size={14} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2 font-mono text-[11px] text-ink-3">
                      <span className="font-bold uppercase tracking-[0.06em] text-ink-2">{kindTitle(item.kind)}</span>
                      <span className="truncate">{item.project_id}</span>
                      <time dateTime={item.ts} title={absoluteTime(item.ts)} className="ml-auto shrink-0">
                        {relativeTime(item.ts, now)}
                      </time>
                    </span>
                    <span className="mt-0.5 block text-[13px] leading-snug text-ink [overflow-wrap:anywhere]">{item.text}</span>
                    {item.realtime && <span className="mt-0.5 block font-mono text-[10px] text-ink-3">also sent to your real-time targets</span>}
                  </span>
                  {!item.read && <span aria-label="unread" className="mt-1.5 h-2 w-2 shrink-0 bg-signal" />}
                </a>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="flex items-center gap-2 border-t border-rule px-4 py-2.5">
        <PixelGlyph name="wire" size={12} className="text-ink-3" />
        <button
          type="button"
          onClick={() => {
            onClose();
            navigate('inbox', { scope: 'needs-you', alerts: '1' });
          }}
          className="font-mono text-[11px] text-ink-2 hover:text-ink hover:underline"
        >
          Real-time alerts: email and signed webhook →
        </button>
      </div>
    </div>
  );
}

export function NotificationBell() {
  const data = useNotifications();
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement | null>(null);
  const panel = useRef<HTMLDivElement | null>(null);
  const titleId = useId();
  const panelId = useId();
  const unread = data.list?.unread ?? 0;
  const badge = badgeText(unread);
  const upto = data.list ? unreadUpto(data.list.items) : {};

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!panel.current?.contains(t) && !button.current?.contains(t)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false);
        button.current?.focus();
      }
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    panel.current?.querySelector<HTMLElement>('a[href], button:not([disabled])')?.focus();
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div className="relative">
      <button
        ref={button}
        type="button"
        onClick={() => {
          if (!open) data.refresh();
          setOpen(!open);
        }}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={badge ? `Notifications, ${unread} unread` : 'Notifications'}
        title={Object.keys(upto).length ? `${unread} unread across ${Object.keys(upto).length} crew${Object.keys(upto).length === 1 ? '' : 's'}` : undefined}
        className="rr-btn-ghost relative inline-flex items-center px-2.5 py-2 text-ink"
      >
        <PixelGlyph name="bell" size={16} />
        {badge && (
          <span aria-hidden="true" className="absolute -right-1 -top-1 min-w-[16px] bg-signal px-[3px] text-center font-mono text-[10px] font-bold leading-4 text-on-signal">
            {badge}
          </span>
        )}
      </button>
      {open && (
        <div
          ref={panel}
          id={panelId}
          role="dialog"
          aria-labelledby={titleId}
          className="rr-card fixed inset-x-3 top-16 z-50 rounded-[3px] sm:absolute sm:inset-x-auto sm:right-0 sm:top-full sm:mt-2 sm:w-[380px]"
        >
          <NotificationCenter data={data} onClose={() => setOpen(false)} titleId={titleId} />
        </div>
      )}
    </div>
  );
}
