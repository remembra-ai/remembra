import { 
  Database, 
  Users, 
  Share2, 
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
  Plus
} from 'lucide-react';
import clsx from 'clsx';

export type TabType = 'memories' | 'entities' | 'graph' | 'decay' | 'debugger' | 'analytics' | 'timeline' | 'keys' | 'billing' | 'settings';

interface NavItem {
  id: TabType;
  label: string;
  icon: React.ElementType;
  section?: string;
}

const navItems: NavItem[] = [
  // Core
  { id: 'memories', label: 'Memories', icon: Database, section: 'core' },
  { id: 'entities', label: 'Entities', icon: Users, section: 'core' },
  { id: 'graph', label: 'Graph', icon: Share2, section: 'core' },
  { id: 'timeline', label: 'Timeline', icon: History, section: 'core' },
  // Insights
  { id: 'analytics', label: 'Analytics', icon: BarChart3, section: 'insights' },
  { id: 'decay', label: 'Decay', icon: TrendingDown, section: 'insights' },
  { id: 'debugger', label: 'Debugger', icon: Bug, section: 'insights' },
  // Account
  { id: 'keys', label: 'API Keys', icon: Key, section: 'account' },
  { id: 'billing', label: 'Billing', icon: CreditCard, section: 'account' },
  { id: 'settings', label: 'Settings', icon: Settings, section: 'account' },
];

interface SidebarProps {
  activeTab: TabType;
  onTabChange: (tab: TabType) => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
  onNewMemory?: () => void;
  onSearch?: () => void;
}

export function Sidebar({ 
  activeTab, 
  onTabChange, 
  collapsed, 
  onToggleCollapse,
  onNewMemory,
  onSearch 
}: SidebarProps) {
  const renderNavSection = (section: string, title: string) => {
    const items = navItems.filter(item => item.section === section);
    
    return (
      <div className="mb-6">
        {!collapsed && (
          <h3 className="px-3 mb-2 text-xs font-semibold text-[hsl(var(--muted-foreground))] uppercase tracking-wider">
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
                  'w-full flex items-center gap-3 px-3 py-2 rounded-lg transition-all duration-150',
                  'text-sm font-medium',
                  isActive
                    ? 'bg-[#8B5CF6]/10 text-[#A78BFA] border-l-2 border-[#8B5CF6]'
                    : 'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]',
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
        'h-screen flex flex-col bg-[hsl(var(--card))] border-r border-[hsl(var(--border))]',
        'transition-all duration-200 ease-in-out',
        collapsed ? 'w-16' : 'w-56'
      )}
    >
      {/* Logo */}
      <div className={clsx(
        'flex items-center h-16 px-4 border-b border-[hsl(var(--border))]',
        collapsed ? 'justify-center' : 'gap-3'
      )}>
        <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-[#8B5CF6] to-[#6D28D9] flex items-center justify-center shadow-lg shadow-purple-500/20">
          <Brain className="w-5 h-5 text-white" />
        </div>
        {!collapsed && (
          <div>
            <h1 className="text-base font-bold text-[hsl(var(--foreground))]">Remembra</h1>
            <p className="text-[10px] text-[hsl(var(--muted-foreground))]">Memory Layer</p>
          </div>
        )}
      </div>

      {/* Quick Actions */}
      <div className={clsx('p-3 space-y-2', collapsed && 'px-2')}>
        {onSearch && (
          <button
            onClick={onSearch}
            className={clsx(
              'w-full flex items-center gap-2 px-3 py-2 rounded-lg',
              'bg-[hsl(var(--muted))] hover:bg-[hsl(var(--muted))]/80',
              'text-[hsl(var(--muted-foreground))] text-sm transition-colors',
              collapsed && 'justify-center px-2'
            )}
          >
            <Search className="w-4 h-4" />
            {!collapsed && (
              <>
                <span className="flex-1 text-left">Search</span>
                <kbd className="text-xs bg-[hsl(var(--background))] px-1.5 py-0.5 rounded">⌘K</kbd>
              </>
            )}
          </button>
        )}
        
        {onNewMemory && (
          <button
            onClick={onNewMemory}
            className={clsx(
              'w-full flex items-center gap-2 px-3 py-2 rounded-lg',
              'bg-[#8B5CF6] hover:bg-[#7C3AED] text-white',
              'text-sm font-medium transition-colors shadow-lg shadow-purple-500/20',
              collapsed && 'justify-center px-2'
            )}
          >
            <Plus className="w-4 h-4" />
            {!collapsed && <span>New Memory</span>}
          </button>
        )}
      </div>

      {/* Navigation */}
      <div className="flex-1 overflow-y-auto px-2 py-4">
        {renderNavSection('core', 'Core')}
        {renderNavSection('insights', 'Insights')}
        {renderNavSection('account', 'Account')}
      </div>

      {/* Collapse Toggle */}
      <div className="p-3 border-t border-[hsl(var(--border))]">
        <button
          onClick={onToggleCollapse}
          className={clsx(
            'w-full flex items-center gap-2 px-3 py-2 rounded-lg',
            'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]',
            'hover:bg-[hsl(var(--muted))] transition-colors text-sm',
            collapsed && 'justify-center px-2'
          )}
        >
          {collapsed ? (
            <ChevronRight className="w-4 h-4" />
          ) : (
            <>
              <ChevronLeft className="w-4 h-4" />
              <span>Collapse</span>
            </>
          )}
        </button>
      </div>
    </aside>
  );
}
