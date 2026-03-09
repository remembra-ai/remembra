import { useState, useEffect } from 'react';
import { Sidebar, type TabType } from './Sidebar';
import { Moon, Sun, LogOut, Search, Menu, X } from 'lucide-react';
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

  return (
    <div className="flex h-screen bg-[hsl(var(--background))] overflow-hidden">
      {/* Mobile Sidebar Backdrop */}
      {mobileMenuOpen && (
        <div 
          className="fixed inset-0 z-40 bg-black/50 md:hidden"
          onClick={() => setMobileMenuOpen(false)}
        />
      )}

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
        <header className="h-14 flex items-center justify-between px-4 md:px-6 border-b border-[hsl(var(--border))] bg-[hsl(var(--card))]/50 backdrop-blur-sm">
          {/* Left: Mobile menu button + Page Title */}
          <div className="flex items-center gap-3">
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
            
            <h1 className="text-lg font-semibold text-[hsl(var(--foreground))] capitalize">
              {activeTab === 'keys' ? 'API Keys' : activeTab}
            </h1>
          </div>

          {/* Right Side Actions */}
          <div className="flex items-center gap-2">
            {/* Quick Search Button */}
            <button
              onClick={onSearch}
              className={clsx(
                'hidden sm:flex items-center gap-2 px-3 py-1.5 rounded-lg',
                'bg-[hsl(var(--muted))] hover:bg-[hsl(var(--muted))]/80',
                'text-[hsl(var(--muted-foreground))] text-sm',
                'transition-colors'
              )}
            >
              <Search className="w-4 h-4" />
              <span>Search</span>
              <kbd className="ml-2 text-xs bg-[hsl(var(--background))] px-1.5 py-0.5 rounded border border-[hsl(var(--border))]">
                ⌘K
              </kbd>
            </button>

            {/* User Info */}
            {isAuthenticated && userName && (
              <div className="hidden md:flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[hsl(var(--muted))]">
                <div className="w-6 h-6 rounded-full bg-gradient-to-br from-[#8B5CF6] to-[#6D28D9] flex items-center justify-center">
                  <span className="text-white text-xs font-medium">
                    {userName.charAt(0).toUpperCase()}
                  </span>
                </div>
                <span className="text-sm text-[hsl(var(--foreground))] max-w-[120px] truncate">
                  {userName}
                </span>
              </div>
            )}

            {/* Theme Toggle */}
            <button
              onClick={onToggleDarkMode}
              className={clsx(
                'p-2 rounded-lg',
                'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]',
                'hover:bg-[hsl(var(--muted))] transition-colors'
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
                  'p-2 rounded-lg',
                  'text-[hsl(var(--muted-foreground))] hover:text-red-500',
                  'hover:bg-[hsl(var(--muted))] transition-colors'
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
          <div className="max-w-7xl mx-auto px-6 py-6">
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
