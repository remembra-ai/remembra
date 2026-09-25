// Primary navigation: a rail on tablets, a labelled sidebar on desktop, and
// a bottom tab bar (plus a "More" sheet) on phones.

import { useEffect, useRef, useState, type ElementType } from 'react';
import clsx from 'clsx';
import {
  Bot,
  Database,
  Ellipsis,
  GitCommitVertical,
  House,
  Inbox,
  Keyboard,
  LogOut,
  Moon,
  Orbit,
  Plug,
  Settings,
  Shield,
  Sun,
  X,
} from 'lucide-react';
import { SECTIONS, hrefFor, sectionOf, type SectionId, type TabType } from '../lib/nav';
import { BrandLockup, BrandMark } from '../brand/Brand';

export type { TabType } from '../lib/nav';

const ICONS: Record<SectionId, ElementType> = {
  home: House,
  trail: GitCommitVertical,
  agents: Bot,
  inbox: Inbox,
  memory: Database,
  graph: Orbit,
  settings: Settings,
  admin: Shield,
};

function visibleSections(isAdmin: boolean) {
  return SECTIONS.filter((section) => !section.adminOnly || isAdmin);
}

function Badge({ count, className }: { count: number; className?: string }) {
  if (count <= 0) return null;
  return (
    <span
      className={clsx(
        'tabular inline-flex min-w-[18px] items-center justify-center rounded-full bg-signal px-1 font-mono text-[10px] font-bold leading-[18px] text-on-signal',
        className,
      )}
    >
      {count > 99 ? '99+' : count}
    </span>
  );
}

interface NavProps {
  activeTab: TabType;
  isAdmin: boolean;
  inboxUnread: number;
}

export function Sidebar({
  activeTab,
  isAdmin,
  inboxUnread,
  userName,
  darkMode,
  onToggleDarkMode,
  onShowShortcuts,
  onOpenConnection,
  onLogout,
}: NavProps & {
  userName?: string;
  darkMode: boolean;
  onToggleDarkMode: () => void;
  onShowShortcuts: () => void;
  onOpenConnection: () => void;
  onLogout: () => void;
}) {
  const active = sectionOf(activeTab).id;
  return (
    <nav
      aria-label="Primary"
      className="hidden h-full w-[68px] shrink-0 flex-col border-r border-rule bg-panel md:flex lg:w-60"
    >
      <a href={hrefFor('home')} className="flex h-16 items-center justify-center px-4 lg:justify-start lg:px-5" aria-label="Remembra home">
        <BrandMark size={34} className="shrink-0 text-ink lg:hidden" />
        <BrandLockup height={27} label="Remembra" className="hidden shrink-0 text-ink lg:block" />
      </a>

      <ul className="flex-1 space-y-0.5 overflow-y-auto px-2.5 py-3">
        {visibleSections(isAdmin).map((section) => {
          const Icon = ICONS[section.id];
          const isActive = active === section.id;
          const badge = section.id === 'inbox' ? inboxUnread : 0;
          return (
            <li key={section.id}>
              <a
                href={hrefFor(section.tabs[0])}
                aria-current={isActive ? 'page' : undefined}
                title={section.label}
                className={clsx(
                  'relative flex items-center gap-3 rounded-[3px] px-3 py-2.5 text-sm font-medium transition-colors',
                  'justify-center lg:justify-start',
                  isActive ? 'bg-paper text-ink' : 'text-ink-2 hover:bg-paper hover:text-ink',
                )}
              >
                {isActive && <span aria-hidden="true" className="absolute inset-y-1.5 left-0 w-[3px] rounded-full bg-signal" />}
                <Icon className="h-[18px] w-[18px] shrink-0" aria-hidden="true" />
                <span className="hidden flex-1 lg:inline">{section.label}</span>
                {badge > 0 && (
                  <>
                    <Badge count={badge} className="hidden lg:inline-flex" />
                    <span aria-hidden="true" className="absolute right-2 top-2 h-2 w-2 rounded-full bg-signal lg:hidden" />
                    <span className="sr-only">, {badge} unread for you</span>
                  </>
                )}
              </a>
            </li>
          );
        })}
      </ul>

      <div className="space-y-0.5 border-t border-rule px-2.5 py-3">
        <button
          type="button"
          onClick={onShowShortcuts}
          title="Keyboard shortcuts (?)"
          className="flex w-full items-center justify-center gap-3 rounded-[3px] px-3 py-2 text-sm text-ink-2 hover:bg-paper hover:text-ink lg:justify-start"
        >
          <Keyboard className="h-[18px] w-[18px] shrink-0" aria-hidden="true" />
          <span className="hidden flex-1 text-left lg:inline">Shortcuts</span>
          <kbd className="hidden rounded-[2px] border border-rule px-1.5 font-mono text-[10px] text-ink-3 lg:inline">?</kbd>
        </button>
        <button
          type="button"
          onClick={onOpenConnection}
          title="Connection and MCP config"
          className="flex w-full items-center justify-center gap-3 rounded-[3px] px-3 py-2 text-sm text-ink-2 hover:bg-paper hover:text-ink lg:justify-start"
        >
          <Plug className="h-[18px] w-[18px] shrink-0" aria-hidden="true" />
          <span className="hidden lg:inline">Connection</span>
        </button>
        <button
          type="button"
          onClick={onToggleDarkMode}
          title={darkMode ? 'Switch to light theme' : 'Switch to dark theme'}
          className="flex w-full items-center justify-center gap-3 rounded-[3px] px-3 py-2 text-sm text-ink-2 hover:bg-paper hover:text-ink lg:justify-start"
        >
          {darkMode ? <Sun className="h-[18px] w-[18px] shrink-0" aria-hidden="true" /> : <Moon className="h-[18px] w-[18px] shrink-0" aria-hidden="true" />}
          <span className="hidden lg:inline">{darkMode ? 'Light theme' : 'Dark theme'}</span>
        </button>
        <button
          type="button"
          onClick={onLogout}
          title="Sign out"
          className="flex w-full items-center justify-center rounded-[3px] px-3 py-2 text-ink-2 hover:bg-paper hover:text-fail lg:hidden"
        >
          <LogOut className="h-[18px] w-[18px]" aria-hidden="true" />
          <span className="sr-only">Sign out</span>
        </button>
        <div className="hidden items-center gap-2 px-1 pt-2 lg:flex">
          <span
            aria-hidden="true"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-ink font-mono text-xs font-bold text-paper"
          >
            {(userName || '?').charAt(0).toUpperCase()}
          </span>
          <span className="min-w-0 flex-1 truncate text-sm text-ink" title={userName}>
            {userName || 'Signed in'}
          </span>
          <button
            type="button"
            onClick={onLogout}
            title="Sign out"
            aria-label="Sign out"
            className="rounded-[3px] p-1.5 text-ink-3 hover:bg-paper hover:text-fail"
          >
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      </div>
    </nav>
  );
}

/** Phone navigation: four primary destinations and a "More" sheet. */
export function MobileNav({
  activeTab,
  isAdmin,
  inboxUnread,
  darkMode,
  onToggleDarkMode,
  onShowShortcuts,
  onOpenConnection,
  onLogout,
  userName,
}: NavProps & {
  darkMode: boolean;
  onToggleDarkMode: () => void;
  onShowShortcuts: () => void;
  onOpenConnection: () => void;
  onLogout: () => void;
  userName?: string;
}) {
  const [moreOpen, setMoreOpen] = useState(false);
  const [lastTab, setLastTab] = useState(activeTab);
  if (lastTab !== activeTab) {
    setLastTab(activeTab);
    setMoreOpen(false);
  }
  const sheetRef = useRef<HTMLDivElement>(null);
  const active = sectionOf(activeTab).id;
  const primary = SECTIONS.filter((s) => ['home', 'trail', 'agents', 'inbox'].includes(s.id));
  const secondary = visibleSections(isAdmin).filter((s) => !['home', 'trail', 'agents', 'inbox'].includes(s.id));
  const moreActive = secondary.some((s) => s.id === active);

  useEffect(() => {
    if (!moreOpen) return undefined;
    sheetRef.current?.querySelector<HTMLElement>('a,button')?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMoreOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [moreOpen]);

  const item = 'flex flex-1 flex-col items-center justify-center gap-0.5 py-1.5 text-[11px] font-medium';
  return (
    <>
      <nav
        aria-label="Primary"
        className="fixed inset-x-0 bottom-0 z-40 border-t border-rule bg-panel/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden"
      >
        <ul className="flex h-16 items-stretch">
          {primary.map((section) => {
            const Icon = ICONS[section.id];
            const isActive = active === section.id;
            const badge = section.id === 'inbox' ? inboxUnread : 0;
            return (
              <li key={section.id} className="flex flex-1">
                <a
                  href={hrefFor(section.tabs[0])}
                  aria-current={isActive ? 'page' : undefined}
                  className={clsx(item, 'relative', isActive ? 'text-ink' : 'text-ink-3')}
                >
                  {isActive && <span aria-hidden="true" className="absolute inset-x-5 top-0 h-[3px] rounded-b-full bg-signal" />}
                  <span className="relative">
                    <Icon className="h-5 w-5" aria-hidden="true" />
                    {badge > 0 && <Badge count={badge} className="absolute -right-3 -top-1.5" />}
                  </span>
                  {section.label}
                  {badge > 0 && <span className="sr-only">, {badge} unread for you</span>}
                </a>
              </li>
            );
          })}
          <li className="flex flex-1">
            <button
              type="button"
              onClick={() => setMoreOpen(true)}
              aria-expanded={moreOpen}
              aria-haspopup="dialog"
              className={clsx(item, 'relative', moreActive ? 'text-ink' : 'text-ink-3')}
            >
              {moreActive && <span aria-hidden="true" className="absolute inset-x-5 top-0 h-[3px] rounded-b-full bg-signal" />}
              <Ellipsis className="h-5 w-5" aria-hidden="true" />
              More
            </button>
          </li>
        </ul>
      </nav>

      {moreOpen && (
        <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-label="More">
          <button type="button" aria-label="Close" className="modal-backdrop absolute inset-0 h-full w-full" onClick={() => setMoreOpen(false)} />
          <div
            ref={sheetRef}
            className="absolute inset-x-0 bottom-0 rounded-t-[6px] border-t border-rule-strong bg-panel pb-[calc(env(safe-area-inset-bottom)+12px)] shadow-[var(--shadow)]"
          >
            <div className="flex items-center justify-between px-4 pb-2 pt-3">
              <span className="truncate font-mono text-xs text-ink-3">{userName}</span>
              <button type="button" onClick={() => setMoreOpen(false)} aria-label="Close" className="rounded-[2px] p-2 text-ink-2">
                <X className="h-5 w-5" />
              </button>
            </div>
            <ul className="grid grid-cols-3 gap-2 px-4">
              {secondary.map((section) => {
                const Icon = ICONS[section.id];
                return (
                  <li key={section.id}>
                    <a
                      href={hrefFor(section.tabs[0])}
                      aria-current={active === section.id ? 'page' : undefined}
                      onClick={() => setMoreOpen(false)}
                      className={clsx(
                        'flex flex-col items-center gap-1.5 rounded-[3px] border px-2 py-3 text-sm',
                        active === section.id ? 'border-ink text-ink' : 'border-rule text-ink-2',
                      )}
                    >
                      <Icon className="h-5 w-5" aria-hidden="true" />
                      {section.label}
                    </a>
                  </li>
                );
              })}
            </ul>
            <div className="mt-3 divide-y divide-rule border-t border-rule">
              <button
                type="button"
                onClick={() => {
                  setMoreOpen(false);
                  onOpenConnection();
                }}
                className="flex w-full items-center gap-3 px-4 py-3 text-left text-sm text-ink"
              >
                <Plug className="h-4 w-4 text-ink-3" aria-hidden="true" /> Connection and MCP config
              </button>
              <button type="button" onClick={onToggleDarkMode} className="flex w-full items-center gap-3 px-4 py-3 text-left text-sm text-ink">
                {darkMode ? <Sun className="h-4 w-4 text-ink-3" aria-hidden="true" /> : <Moon className="h-4 w-4 text-ink-3" aria-hidden="true" />}
                {darkMode ? 'Light theme' : 'Dark theme'}
              </button>
              <button
                type="button"
                onClick={() => {
                  setMoreOpen(false);
                  onShowShortcuts();
                }}
                className="flex w-full items-center gap-3 px-4 py-3 text-left text-sm text-ink"
              >
                <Keyboard className="h-4 w-4 text-ink-3" aria-hidden="true" /> Keyboard shortcuts
              </button>
              <button type="button" onClick={onLogout} className="flex w-full items-center gap-3 px-4 py-3 text-left text-sm text-fail">
                <LogOut className="h-4 w-4" aria-hidden="true" /> Sign out
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
