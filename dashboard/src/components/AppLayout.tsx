import { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Sidebar, type TabType } from './Sidebar';
import { ProjectSwitcher } from './ProjectSwitcher';
import { SettingsPanel } from './SettingsPanel';
import { Moon, Sun, LogOut, Search, Menu, X, Wifi, Sparkles, Command, Settings } from 'lucide-react';
import clsx from 'clsx';

interface AppLayoutProps {
  children: React.ReactNode;
  activeTab: TabType;
  onTabChange: (tab: TabType) => void;
  darkMode: boolean;
  onToggleDarkMode: () => void;
  isAuthenticated: boolean;
  onLogout: () => void;
  userName?: string;
  onNewMemory?: () => void;
  onSearch?: () => void;
}

export function AppLayout({
  children,
  activeTab,
  onTabChange,
  darkMode,
  onToggleDarkMode,
  isAuthenticated,
  onLogout,
  userName,
  onNewMemory,
  onSearch,
}: AppLayoutProps) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    const saved = localStorage.getItem('sidebarCollapsed');
    return saved === 'true';
  });
  
  // Mobile sidebar state
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  
  // Settings panel state
  const [settingsPanelOpen, setSettingsPanelOpen] = useState(false);

  // Save sidebar state
  useEffect(() => {
    localStorage.setItem('sidebarCollapsed', String(sidebarCollapsed));
  }, [sidebarCollapsed]);

  // Keyboard shortcut for search (⌘K)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        onSearch?.();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onSearch]);

  // Close mobile menu on tab change
  const handleTabChange = (tab: TabType) => {
    onTabChange(tab);
    setMobileMenuOpen(false);
  };

  // Close mobile menu on escape
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMobileMenuOpen(false);
    };
    window.addEventListener('keydown', handleEscape);
    return () => window.removeEventListener('keydown', handleEscape);
  }, []);

  const tabMeta: Record<TabType, { title: string; subtitle: string }> = {
    memories: {
      title: 'Memory Nexus',
      subtitle: 'Search, store, and shape the working memory of your agents.',
    },
    projects: {
      title: 'Projects',
      subtitle: 'Partition memory into focused workspaces with clear boundaries.',
    },
    entities: {
      title: 'Entity Atlas',
      subtitle: 'Track the people, products, and concepts your memory graph resolves.',
    },
    graph: {
      title: 'Knowledge Graph',
      subtitle: 'Explore how memories connect across entities, time, and teams.',
    },
    timeline: {
      title: 'Timeline',
      subtitle: 'Follow memory creation, change, and relevance across the project.',
    },
    analytics: {
      title: 'Analytics',
      subtitle: 'Watch recall traffic, storage growth, and usage patterns in real time.',
    },
    decay: {
      title: 'Decay Report',
      subtitle: 'Inspect recency, retention, and what your system is forgetting.',
    },
    debugger: {
      title: 'Query Debugger',
      subtitle: 'Interrogate retrieval quality, ranking, and model-facing context.',
    },
    teams: {
      title: 'Teams',
      subtitle: 'Coordinate shared memory across users, roles, and agents.',
    },
    keys: {
      title: 'API Keys',
      subtitle: 'Manage secure access for apps, agents, and automation.',
    },
    billing: {
      title: 'Billing',
      subtitle: 'Track plan usage, upgrade levers, and commercial health.',
    },
    settings: {
      title: 'Settings',
      subtitle: 'Tune workspace defaults, preferences, and dashboard behavior.',
    },
    admin: {
      title: 'Admin',
      subtitle: 'Operate the control plane with clear visibility and guardrails.',
    },
  };

  const activeMeta = tabMeta[activeTab];
  const contentShellClass = activeTab === 'graph'
    ? 'page-enter px-3 py-4 md:px-4 md:py-5'
    : 'page-enter max-w-7xl mx-auto px-4 py-6 md:px-6 md:py-7';

  return (
    <div className="dashboard-shell flex h-screen bg-[hsl(var(--background))] overflow-hidden">
      {/* Mobile Sidebar Backdrop */}
      <AnimatePresence>
        {mobileMenuOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm md:hidden"
            onClick={() => setMobileMenuOpen(false)}
          />
        )}
      </AnimatePresence>

      {/* Sidebar - hidden on mobile unless menu is open */}
      <div className={clsx(
        'md:relative md:block',
        // Mobile: fixed overlay
        'fixed inset-y-0 left-0 z-50 md:z-auto',
        'transform transition-transform duration-200 ease-in-out md:transform-none',
        mobileMenuOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'
      )}>
        <Sidebar
          activeTab={activeTab}
          onTabChange={handleTabChange}
          collapsed={sidebarCollapsed}
          onToggleCollapse={() => setSidebarCollapsed(!sidebarCollapsed)}
          onNewMemory={onNewMemory}
          onSearch={onSearch}
        />
      </div>

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Top Header Bar */}
        <header className="dashboard-surface relative z-40 mx-3 mt-3 h-20 rounded-[26px] flex items-center justify-between px-4 md:px-6">
          {/* Left: Mobile menu button + Page Title */}
          <div className="flex items-center gap-3 min-w-0">
            {/* Mobile hamburger menu */}
            <button
              onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
              className={clsx(
                'p-2 rounded-lg md:hidden',
                'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]',
                'hover:bg-[hsl(var(--muted))] transition-colors'
              )}
              aria-label="Toggle menu"
            >
              {mobileMenuOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
            </button>
            
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.28em] text-[hsl(var(--muted-foreground))]">
                <Sparkles className="w-3.5 h-3.5 text-[hsl(var(--shell-glow))]" />
                Workspace / Remembra
              </div>
              <div className="flex items-center gap-3 min-w-0">
                <h1 className="text-xl font-semibold text-[hsl(var(--foreground))] truncate">
                  {activeMeta.title}
                </h1>
                <div className="hidden lg:flex items-center gap-2 premium-chip rounded-full px-3 py-1 text-xs text-[hsl(var(--muted-foreground))]">
                  <Wifi className="w-3.5 h-3.5 text-emerald-400" />
                  Realtime sync
                </div>
              </div>
              <p className="hidden md:block text-sm text-[hsl(var(--muted-foreground))] truncate">
                {activeMeta.subtitle}
              </p>
            </div>
          </div>

          {/* Right Side Actions */}
          <div className="flex items-center gap-2">
            {/* Project Switcher */}
            <ProjectSwitcher onProjectChange={() => {
              // Project data will reload automatically via page refresh in ProjectSwitcher
            }} />

            {/* Quick Search Button */}
            <button
              onClick={onSearch}
              className={clsx(
                'hidden sm:flex items-center gap-2 px-3 py-2 rounded-xl premium-chip',
                'hover:bg-[hsl(var(--muted))/0.82]',
                'text-[hsl(var(--muted-foreground))] text-sm',
                'transition-colors'
              )}
            >
              <Search className="w-4 h-4" />
              <span className="hidden md:inline">Search</span>
              <kbd className="ml-2 inline-flex items-center gap-1 text-xs bg-[hsl(var(--background))/0.95] px-1.5 py-0.5 rounded-md border border-[hsl(var(--border))/0.85]">
                <Command className="w-3 h-3" />
                ⌘K
              </kbd>
            </button>

            <div className="hidden xl:flex items-center gap-2 premium-chip rounded-full px-3 py-2 text-xs text-[hsl(var(--muted-foreground))]">
              <span className="h-2 w-2 rounded-full bg-[hsl(var(--shell-glow))]" />
              Enterprise
            </div>

            {/* User Info */}
            {isAuthenticated && userName && (
              <div className="hidden md:flex items-center gap-2 px-3 py-2 rounded-xl premium-chip">
                <div className="w-7 h-7 rounded-full bg-[linear-gradient(135deg,hsl(var(--primary)),hsl(var(--shell-glow)))] flex items-center justify-center shadow-[0_12px_24px_hsl(var(--primary)/0.24)]">
                  <span className="text-white text-xs font-medium">
                    {userName.charAt(0).toUpperCase()}
                  </span>
                </div>
                <span className="text-sm text-[hsl(var(--foreground))] max-w-[120px] truncate">
                  {userName}
                </span>
              </div>
            )}

            {/* Settings Button */}
            <button
              onClick={() => setSettingsPanelOpen(true)}
              className={clsx(
                'p-2 rounded-xl premium-chip',
                'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]',
                'hover:bg-[hsl(var(--muted))/0.82] transition-colors'
              )}
              title="Settings"
            >
              <Settings className="w-5 h-5" />
            </button>

            {/* Theme Toggle */}
            <button
              onClick={onToggleDarkMode}
              className={clsx(
                'p-2 rounded-xl premium-chip',
                'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]',
                'hover:bg-[hsl(var(--muted))/0.82] transition-colors'
              )}
              title={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
            >
              {darkMode ? <Sun className="w-5 h-5" /> : <Moon className="w-5 h-5" />}
            </button>

            {/* Logout */}
            {isAuthenticated && (
              <button
                onClick={onLogout}
                className={clsx(
                  'p-2 rounded-xl premium-chip',
                  'text-[hsl(var(--muted-foreground))] hover:text-red-500',
                  'hover:bg-[hsl(var(--muted))/0.82] transition-colors'
                )}
                title="Logout"
              >
                <LogOut className="w-5 h-5" />
              </button>
            )}
          </div>
        </header>

        {/* Page Content */}
        <main className="flex-1 overflow-auto">
          <div key={activeTab} className={contentShellClass}>
            {children}
          </div>
        </main>
      </div>

      {/* Settings Panel */}
      <SettingsPanel
        isOpen={settingsPanelOpen}
        onClose={() => setSettingsPanelOpen(false)}
        onLogout={onLogout}
        onOpenApiKeys={() => {
          setSettingsPanelOpen(false);
          onTabChange('keys');
        }}
      />
    </div>
  );
}
