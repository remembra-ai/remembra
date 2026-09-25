import { useState, type ReactNode } from 'react';
import clsx from 'clsx';
import { Search } from 'lucide-react';
import { MobileNav, Sidebar } from './Sidebar';
import { ProjectSwitcher } from './ProjectSwitcher';
import { SettingsPanel } from './SettingsPanel';
import { BrandMark } from './relay/ui';
import { TABS, hrefFor, sectionOf, type TabType } from '../lib/nav';

interface AppLayoutProps {
  children: ReactNode;
  activeTab: TabType;
  darkMode: boolean;
  onToggleDarkMode: () => void;
  onLogout: () => void;
  userName?: string;
  onSearch: () => void;
  onShowShortcuts: () => void;
  isAdmin?: boolean;
  inboxUnread: number;
}

export function AppLayout({
  children,
  activeTab,
  darkMode,
  onToggleDarkMode,
  onLogout,
  userName,
  onSearch,
  onShowShortcuts,
  isAdmin = false,
  inboxUnread,
}: AppLayoutProps) {
  const [connectionOpen, setConnectionOpen] = useState(false);
  const section = sectionOf(activeTab);
  const meta = TABS[activeTab];
  const subTabs = section.tabs.length > 1 ? section.tabs : [];
  const usesMemoryProject = section.id === 'memory' || section.id === 'graph';
  const wide = activeTab === 'graph';

  const navProps = {
    activeTab,
    isAdmin,
    inboxUnread,
    darkMode,
    onToggleDarkMode,
    onShowShortcuts,
    onOpenConnection: () => setConnectionOpen(true),
    onLogout,
    userName,
  };

  return (
    <div className="dashboard-shell flex h-dvh overflow-hidden">
      <a
        href="#main"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById('main')?.focus();
        }}
        className="sr-only-focusable fixed left-3 top-3 z-[60] rounded-[3px] bg-ink px-3 py-2 text-sm text-paper"
      >
        Skip to content
      </a>

      <Sidebar {...navProps} />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center gap-3 border-b border-rule bg-paper px-4 md:h-16 md:px-6">
          <a href={hrefFor('home')} className="md:hidden" aria-label="Remembra home">
            <BrandMark size={26} className="text-ink" />
          </a>
          <div className="min-w-0 flex-1">
            <h1 className="font-display truncate text-lg font-bold leading-tight tracking-[-0.02em] text-ink md:text-xl">{meta.title}</h1>
            <p className="hidden truncate text-xs text-ink-3 md:block">{meta.subtitle}</p>
          </div>
          {usesMemoryProject && (
            <div className="hidden sm:block">
              <ProjectSwitcher />
            </div>
          )}
          <button
            type="button"
            onClick={onSearch}
            className="rr-btn-ghost inline-flex items-center gap-2 px-2.5 py-2 text-sm"
            aria-label="Search and commands"
            aria-keyshortcuts="Meta+K Control+K"
          >
            <Search className="h-4 w-4" aria-hidden="true" />
            <span className="hidden md:inline">Search</span>
            <kbd className="hidden rounded-[2px] border border-rule px-1.5 font-mono text-[10px] text-ink-3 md:inline">⌘K</kbd>
          </button>
        </header>

        {subTabs.length > 0 && (
          <div className="shrink-0 border-b border-rule bg-paper">
            <nav aria-label={`${section.label} pages`} className="scrollbar-hide flex gap-1 overflow-x-auto px-4 md:px-6">
              {subTabs.map((tab) => (
                <a
                  key={tab}
                  href={hrefFor(tab)}
                  aria-current={tab === activeTab ? 'page' : undefined}
                  className={clsx(
                    'relative shrink-0 px-2.5 py-2.5 text-sm font-medium transition-colors',
                    tab === activeTab ? 'text-ink' : 'text-ink-3 hover:text-ink',
                  )}
                >
                  {TABS[tab].label}
                  {tab === activeTab && <span aria-hidden="true" className="absolute inset-x-2 bottom-0 h-[3px] bg-signal" />}
                </a>
              ))}
            </nav>
            {usesMemoryProject && (
              <div className="px-4 pb-2 sm:hidden">
                <ProjectSwitcher />
              </div>
            )}
          </div>
        )}

        <main id="main" tabIndex={-1} className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden pb-[calc(76px+env(safe-area-inset-bottom))] outline-none md:pb-0">
          <div
            key={activeTab}
            className={clsx('page-enter', wide ? 'px-3 py-4 md:px-4' : 'mx-auto max-w-6xl px-4 py-5 md:px-6 md:py-6')}
          >
            {children}
          </div>
        </main>
      </div>

      <MobileNav {...navProps} />

      <SettingsPanel
        isOpen={connectionOpen}
        onClose={() => setConnectionOpen(false)}
        onLogout={onLogout}
        onOpenApiKeys={() => {
          setConnectionOpen(false);
          window.location.hash = hrefFor('keys');
        }}
      />
    </div>
  );
}
