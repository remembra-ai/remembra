import { 
  Database, 
  Users, 
  UsersRound,
  TrendingDown, 
  Bug, 
  BarChart3, 
  History, 
  Key, 
  CreditCard, 
  Settings,
  ChevronLeft,
  ChevronRight,
  Brain,
  Search,
  Plus,
  Shield,
  FolderOpen,
  Activity,
  Command,
  Orbit
} from 'lucide-react';
import clsx from 'clsx';

export type TabType = 'memories' | 'entities' | 'graph' | 'brain' | 'decay' | 'debugger' | 'analytics' | 'timeline' | 'projects' | 'keys' | 'billing' | 'settings' | 'teams' | 'admin';

interface NavItem {
  id: TabType;
  label: string;
  icon: React.ElementType;
  section?: string;
}

const navItems: NavItem[] = [
  // Memory
  { id: 'memories', label: 'Control Plane', icon: Database, section: 'memory' },
  { id: 'entities', label: 'Entities', icon: Users, section: 'memory' },
  { id: 'projects', label: 'Projects', icon: FolderOpen, section: 'memory' },
  // Exploration
  { id: 'graph', label: 'Knowledge Graph', icon: Orbit, section: 'explore' },
  { id: 'brain', label: 'Brain', icon: Brain, section: 'explore' },
  { id: 'timeline', label: 'Timeline', icon: History, section: 'explore' },
  { id: 'analytics', label: 'Analytics', icon: BarChart3, section: 'explore' },
  // Developer Tools
  { id: 'decay', label: 'Decay Report', icon: TrendingDown, section: 'tools' },
  { id: 'debugger', label: 'Debugger', icon: Bug, section: 'tools' },
  // Settings & Account
  { id: 'settings', label: 'Settings', icon: Settings, section: 'account' },
  { id: 'keys', label: 'API Keys', icon: Key, section: 'account' },
  { id: 'billing', label: 'Billing', icon: CreditCard, section: 'account' },
  { id: 'teams', label: 'Teams', icon: UsersRound, section: 'account' },
  // Admin (superadmin only)
  { id: 'admin', label: 'Admin', icon: Shield, section: 'admin' },
];

interface SidebarProps {
  activeTab: TabType;
  onTabChange: (tab: TabType) => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
  onNewMemory?: () => void;
  onSearch?: () => void;
  isAdmin?: boolean;
}

export function Sidebar({ 
  activeTab, 
  onTabChange, 
  collapsed, 
  onToggleCollapse,
  onNewMemory,
  onSearch,
  isAdmin = false 
}: SidebarProps) {
  const activeItem = navItems.find((item) => item.id === activeTab);

  const renderNavSection = (section: string, title: string) => {
    const items = navItems.filter(item => item.section === section);
    
    return (
      <div className="mb-7">
        {!collapsed && (
          <h3 className="px-3 mb-2 text-[10px] font-semibold text-[hsl(var(--muted-foreground))] uppercase tracking-[0.28em]">
            {title}
          </h3>
        )}
        <nav className="space-y-1">
          {items.map((item) => {
            const Icon = item.icon;
            const isActive = activeTab === item.id;
            
            return (
              <button
                key={item.id}
                onClick={() => onTabChange(item.id)}
                className={clsx(
                  'w-full flex items-center gap-3 px-3 py-2.5 rounded-xl transition-all duration-150',
                  'text-sm font-medium',
                  isActive
                    ? 'bg-[linear-gradient(135deg,hsl(var(--primary))/0.18,hsl(var(--shell-glow))/0.08)] text-[hsl(var(--foreground))] shadow-[inset_0_1px_0_hsl(0_0%_100%/0.04),0_10px_24px_hsl(0_0%_0%/0.14)] ring-1 ring-[hsl(var(--primary))/0.24]'
                    : 'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))/0.78]',
                  collapsed && 'justify-center px-2'
                )}
                title={collapsed ? item.label : undefined}
              >
                <Icon className={clsx('flex-shrink-0', collapsed ? 'w-5 h-5' : 'w-4 h-4')} />
                {!collapsed && <span>{item.label}</span>}
              </button>
            );
          })}
        </nav>
      </div>
    );
  };

  return (
    <aside 
      className={clsx(
        'h-screen flex flex-col dashboard-surface',
        'transition-all duration-200 ease-in-out',
        collapsed ? 'w-16' : 'w-56'
      )}
    >
      {/* Logo */}
      <div className={clsx(
        'flex items-center min-h-20 px-4 border-b border-[hsl(var(--border))/0.72]',
        collapsed ? 'justify-center' : 'gap-3'
      )}>
        <div className="w-9 h-9 rounded-2xl bg-[linear-gradient(135deg,hsl(var(--primary)),hsl(var(--shell-glow)))] flex items-center justify-center shadow-[0_14px_28px_hsl(var(--primary)/0.28)]">
          <Brain className="w-5 h-5 text-white" />
        </div>
        {!collapsed && (
          <div className="min-w-0">
            <p className="text-[10px] uppercase tracking-[0.26em] text-[hsl(var(--muted-foreground))]">
              Remembra
            </p>
            <h1 className="text-base font-semibold text-[hsl(var(--foreground))]">Control Plane</h1>
            <p className="text-[11px] text-[hsl(var(--muted-foreground))] truncate">
              Shared agent memory
            </p>
          </div>
        )}
      </div>

      {/* Quick Actions */}
      <div className={clsx('p-3 space-y-2.5', collapsed && 'px-2')}>
        {onSearch && (
          <button
            onClick={onSearch}
            className={clsx(
              'w-full flex items-center gap-2 px-3 py-2.5 rounded-xl',
              'premium-chip hover:bg-[hsl(var(--muted))/0.82]',
              'text-[hsl(var(--muted-foreground))] text-sm transition-colors',
              collapsed && 'justify-center px-2'
            )}
          >
            <Search className="w-4 h-4" />
            {!collapsed && (
              <>
                <span className="flex-1 text-left">Search</span>
                <kbd className="text-xs bg-[hsl(var(--background))/0.9] px-1.5 py-0.5 rounded-md border border-[hsl(var(--border))/0.85]">⌘K</kbd>
              </>
            )}
          </button>
        )}
        
        {onNewMemory && (
          <button
            onClick={onNewMemory}
            className={clsx(
              'w-full flex items-center gap-2 px-3 py-2.5 rounded-xl',
              'bg-[linear-gradient(135deg,hsl(var(--primary)),hsl(var(--shell-glow)))] text-white',
              'text-sm font-medium transition-transform duration-150 hover:scale-[1.01]',
              'shadow-[0_18px_36px_hsl(var(--primary)/0.22)]',
              collapsed && 'justify-center px-2'
            )}
          >
            <Plus className="w-4 h-4" />
            {!collapsed && <span>New Memory</span>}
          </button>
        )}

        {!collapsed && (
          <div className="premium-chip rounded-2xl px-3 py-3.5">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.24em] text-[hsl(var(--muted-foreground))]">
                <Activity className="w-3.5 h-3.5 text-emerald-400" />
                Live Sync
              </div>
              <div className="flex items-center gap-1.5 rounded-full border border-emerald-400/24 bg-emerald-400/8 px-2 py-1 text-[10px] font-medium text-emerald-300">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 sync-pulse" />
                Healthy
              </div>
            </div>
            <p className="mt-2 text-xs leading-5 text-[hsl(var(--foreground))]">
              {activeItem ? `${activeItem.label} is in focus.` : 'Workspace ready.'}
            </p>
          </div>
        )}
      </div>

      {/* Navigation */}
      <div className="flex-1 overflow-y-auto px-2 py-4">
        {renderNavSection('memory', 'Memory')}
        {renderNavSection('explore', 'Explore')}
        {renderNavSection('tools', 'Dev Tools')}
        {renderNavSection('account', 'Account')}
        {isAdmin === true && renderNavSection('admin', 'Admin')}
      </div>

      {/* Collapse Toggle */}
      <div className="p-3 border-t border-[hsl(var(--border))/0.72]">
        {!collapsed && (
          <div className="mb-2 rounded-2xl premium-chip px-3 py-3">
            <div className="flex items-center gap-2 text-xs font-medium text-[hsl(var(--foreground))]">
              <Orbit className="w-4 h-4 text-[hsl(var(--shell-glow))]" />
              Memory orbit is stable
            </div>
            <div className="mt-2 flex items-center gap-2 text-[11px] text-[hsl(var(--muted-foreground))]">
              <Command className="w-3.5 h-3.5" />
              Keyboard-first shell
            </div>
          </div>
        )}
        <button
          onClick={onToggleCollapse}
          className={clsx(
            'w-full flex items-center gap-2 px-3 py-2.5 rounded-xl',
            'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]',
            'hover:bg-[hsl(var(--muted))/0.78] transition-colors text-sm',
            collapsed && 'justify-center px-2'
          )}
        >
          {collapsed ? (
            <ChevronRight className="w-4 h-4" />
          ) : (
            <>
              <ChevronLeft className="w-4 h-4" />
              <span>Collapse Rail</span>
            </>
          )}
        </button>
      </div>
    </aside>
  );
}
