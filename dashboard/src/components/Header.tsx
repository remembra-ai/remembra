import { Moon, Sun, LogOut, User } from 'lucide-react';

interface HeaderProps {
  darkMode: boolean;
  onToggleDarkMode: () => void;
  isAuthenticated: boolean;
  onLogout: () => void;
  userName?: string;
}

export function Header({ darkMode, onToggleDarkMode, isAuthenticated, onLogout, userName }: HeaderProps) {
  return (
    <header className="bg-[hsl(var(--card))] border-b border-[hsl(var(--border))]">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16">
          {/* Logo */}
          <div className="flex items-center gap-3">
            <img 
              src="/logo.jpg" 
              alt="Remembra" 
              className="w-10 h-10 rounded-lg object-cover"
            />
            <div>
              <h1 className="text-xl font-bold text-[hsl(var(--foreground))]">
                Remembra
              </h1>
              <p className="text-xs text-[hsl(var(--muted-foreground))]">
                Memory Dashboard
              </p>
            </div>
          </div>

          {/* Actions */}
          <div className="flex items-center gap-3">
            {/* User info */}
            {isAuthenticated && userName && (
              <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[hsl(var(--muted))]">
                <User className="w-4 h-4 text-[hsl(var(--muted-foreground))]" />
                <span className="text-sm text-[hsl(var(--foreground))] max-w-[150px] truncate">
                  {userName}
                </span>
              </div>
            )}

            <button
              onClick={onToggleDarkMode}
              className="p-2 rounded-lg text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))] transition-colors"
              title={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
            >
              {darkMode ? (
                <Sun className="w-5 h-5" />
              ) : (
                <Moon className="w-5 h-5" />
              )}
            </button>

            {isAuthenticated && (
              <button
                onClick={onLogout}
                className="p-2 rounded-lg text-[hsl(var(--muted-foreground))] hover:text-red-500 hover:bg-[hsl(var(--muted))] transition-colors"
                title="Logout"
              >
                <LogOut className="w-5 h-5" />
              </button>
            )}
          </div>
        </div>
      </div>
    </header>
  );
}
