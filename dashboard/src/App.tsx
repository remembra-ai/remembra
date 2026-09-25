import { useState, useEffect, useCallback } from 'react';
import { AppLayout } from './components/AppLayout';
import { ApiKeyForm } from './components/ApiKeyForm';
import { CommandPalette } from './components/CommandPalette';
import { ShortcutsDialog } from './components/ShortcutsDialog';
import { RelayDataProvider } from './components/relay/RelayDataProvider';
import { Dashboard } from './pages/Dashboard';
import { Home } from './pages/Home';
import { Trail } from './pages/Trail';
import { Agents } from './pages/Agents';
import { Inbox } from './pages/Inbox';
import { useRelayData } from './hooks/relayData';
import { useShortcuts } from './hooks/useShortcuts';
import { navigate, useRoute, type TabType } from './lib/nav';
import { Login } from './pages/Login';
import { Signup } from './pages/Signup';
import { ForgotPassword } from './pages/ForgotPassword';
import { InviteAccept } from './pages/InviteAccept';
import { api } from './lib/api';
import { API_V1 } from './config';

type AuthMode = 'login' | 'signup' | 'forgot-password' | 'reset-password' | 'api-key' | 'invite';

function App() {
  const [darkMode, setDarkMode] = useState(() => {
    try {
      const saved = localStorage.getItem('darkMode');
      if (saved === 'true' || saved === 'false') return saved === 'true';
    } catch {
      // Storage unavailable: follow the system theme.
    }
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
  });
  const [themeChosen, setThemeChosen] = useState(() => {
    try {
      return localStorage.getItem('darkMode') !== null;
    } catch {
      return false;
    }
  });
  
  const [isAuthenticated, setIsAuthenticated] = useState(() => {
    // Check for JWT token first, then API key
    return !!localStorage.getItem('remembra_jwt_token') || !!api.getApiKey();
  });
  
  const [inviteToken, setInviteToken] = useState<string | null>(() => {
    // Check for invite URL: /invite/:token
    const path = window.location.pathname;
    const match = path.match(/^\/invite\/(.+)$/);
    return match ? match[1] : localStorage.getItem('pending_invite_token');
  });

  const [authMode, setAuthMode] = useState<AuthMode>(() => {
    // Check URL path to determine initial auth mode
    const path = window.location.pathname;
    if (path.startsWith('/invite/')) return 'invite';
    if (path === '/signup') return 'signup';
    if (path === '/forgot-password') return 'forgot-password';
    if (path === '/reset-password') return 'reset-password';
    return 'login';
  });
  const [currentUser, setCurrentUser] = useState<{ id: string; email: string; name?: string; is_admin?: boolean } | null>(() => {
    const saved = localStorage.getItem('remembra_user');
    return saved ? JSON.parse(saved) : null;
  });

  const { tab: activeTab } = useRoute();
  const setActiveTab = useCallback((tab: TabType) => navigate(tab), []);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', darkMode);
    if (!themeChosen) return;
    try {
      localStorage.setItem('darkMode', String(darkMode));
    } catch {
      // Storage unavailable: the choice lasts for this visit.
    }
  }, [darkMode, themeChosen]);

  // Follow the system theme until the user picks one.
  useEffect(() => {
    if (themeChosen || !window.matchMedia) return undefined;
    const query = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = (e: MediaQueryListEvent) => setDarkMode(e.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, [themeChosen]);

  // Verify JWT token on mount
  useEffect(() => {
    const verifyToken = async () => {
      const token = localStorage.getItem('remembra_jwt_token');
      if (!token) return;

      try {
        const response = await fetch(`${API_V1}/auth/me`, {
          headers: {
            'Authorization': `Bearer ${token}`,
          },
        });

        if (!response.ok) {
          // Token invalid, clear auth
          localStorage.removeItem('remembra_jwt_token');
          localStorage.removeItem('remembra_user');
          api.clearAll();
          setCurrentUser(null);
          setIsAuthenticated(false);
          setAuthMode('login');
        } else {
          const user = await response.json();
          setCurrentUser({ id: user.id, email: user.email, name: user.name, is_admin: user.is_admin });
          localStorage.setItem('remembra_user', JSON.stringify(user));
          // Set user ID in API client for API calls
          api.setUserId(user.id);
          api.setJwtToken(token);
          setIsAuthenticated(true);
        }
      } catch {
        // Network error, keep existing state
      }
    };

    verifyToken();
  }, []);

  const handleToggleDarkMode = () => {
    setThemeChosen(true);
    setDarkMode(!darkMode);
  };

  const handleLogin = async (token: string, user: { id: string; email: string; name?: string; is_admin?: boolean }) => {
    localStorage.setItem('remembra_jwt_token', token);
    localStorage.setItem('remembra_user', JSON.stringify(user));
    // Set user ID in API client for compatibility
    api.setUserId(user.id);
    api.setJwtToken(token);
    setCurrentUser({ ...user, is_admin: user.is_admin ?? false });
    setIsAuthenticated(true);
    
    // Check for pending invite
    const pendingInvite = localStorage.getItem('pending_invite_token');
    if (pendingInvite) {
      // Auto-accept the invite
      try {
        const response = await fetch(`${API_V1}/teams/invites/accept`, {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ token: pendingInvite }),
        });
        
        if (response.ok) {
          localStorage.removeItem('pending_invite_token');
          setInviteToken(null);
          // Clear the invite path, then open Teams
          window.history.replaceState({}, '', '/');
          setActiveTab('teams');
          return;
        }
      } catch {
        // Ignore errors, user can manually accept
      }
      localStorage.removeItem('pending_invite_token');
    }
    
    // Check if user had a plan intent (signed up via pricing page with plan=pro/team)
    const planIntent = localStorage.getItem('remembra_plan_intent');
    if (planIntent && (planIntent === 'pro' || planIntent === 'team')) {
      localStorage.removeItem('remembra_plan_intent');
      // Redirect to billing tab after login
      setActiveTab('billing');
    }
  };

  const handleSignup = (user: { id: string; email: string; name?: string }) => {
    // After signup, switch to login
    // Check if user signed up with a paid plan intent (plan param in URL)
    const urlParams = new URLSearchParams(window.location.search);
    const planIntent = urlParams.get('plan');
    if (planIntent && (planIntent === 'pro' || planIntent === 'team')) {
      // Store plan intent for after login - user will be prompted to upgrade
      localStorage.setItem('remembra_plan_intent', planIntent);
    }
    setCurrentUser(user);
    setAuthMode('login');
  };

  const handleApiKeyAuth = () => {
    setIsAuthenticated(true);
  };

  const handleLogout = async () => {
    const token = localStorage.getItem('remembra_jwt_token');
    
    // Call logout endpoint if we have a JWT token
    if (token) {
      try {
        await fetch(`${API_V1}/auth/logout`, {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${token}`,
          },
        });
      } catch {
        // Ignore errors on logout
      }
    }

    // Clear all auth state
    localStorage.removeItem('remembra_jwt_token');
    localStorage.removeItem('remembra_user');
    api.clearAll();
    setCurrentUser(null);
    setIsAuthenticated(false);
    setAuthMode('login');
  };

  // Command palette + shortcuts state
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [showNewMemoryModal, setShowNewMemoryModal] = useState(false);

  const handleSearch = useCallback(() => {
    setCommandPaletteOpen(true);
  }, []);

  useShortcuts({
    enabled: isAuthenticated && !inviteToken && !commandPaletteOpen && !shortcutsOpen,
    onPalette: handleSearch,
    onHelp: () => setShortcutsOpen(true),
  });

  // Not authenticated - show auth screens
  if (!isAuthenticated) {
    return (
      <div className={darkMode ? 'dark' : ''}>
        {authMode === 'login' && (
          <Login
            onLogin={handleLogin}
            onSwitchToSignup={() => setAuthMode('signup')}
            onForgotPassword={() => setAuthMode('forgot-password')}
          />
        )}
        {authMode === 'signup' && (
          <Signup
            onSignup={handleSignup}
            onSwitchToLogin={() => setAuthMode('login')}
          />
        )}
        {authMode === 'forgot-password' && (
          <ForgotPassword
            onBackToLogin={() => setAuthMode('login')}
          />
        )}
        {authMode === 'reset-password' && (
          <ForgotPassword
            onBackToLogin={() => setAuthMode('login')}
            initialStep="reset"
          />
        )}
        {authMode === 'api-key' && (
          <ApiKeyForm onAuthenticated={handleApiKeyAuth} />
        )}
        {authMode === 'invite' && inviteToken && (
          <InviteAccept
            token={inviteToken}
            isAuthenticated={false}
            onAccepted={() => {
              window.history.replaceState({}, '', '/');
              setActiveTab('teams');
            }}
            onSwitchToLogin={() => {
              localStorage.setItem('pending_invite_token', inviteToken);
              setAuthMode('login');
            }}
            onSwitchToSignup={() => {
              localStorage.setItem('pending_invite_token', inviteToken);
              setAuthMode('signup');
            }}
          />
        )}
        
        {/* Toggle between user auth and API key auth */}
        {authMode !== 'api-key' && authMode !== 'invite' && (
          <div className="fixed bottom-4 right-4">
            <button
              onClick={() => setAuthMode('api-key')}
              className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 underline"
            >
              Use API Key instead
            </button>
          </div>
        )}
        {authMode === 'api-key' && (
          <div className="fixed bottom-4 right-4">
            <button
              onClick={() => setAuthMode('login')}
              className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 underline"
            >
              Sign in with email
            </button>
          </div>
        )}
      </div>
    );
  }

  // Check if authenticated user is on an invite page
  if (inviteToken) {
    return (
      <div className={darkMode ? 'dark' : ''}>
        <InviteAccept
          token={inviteToken}
          isAuthenticated={true}
          onAccepted={() => {
            setInviteToken(null);
            window.history.replaceState({}, '', '/');
            setActiveTab('teams');
          }}
          onSwitchToLogin={() => {}}
          onSwitchToSignup={() => {}}
        />
      </div>
    );
  }

  // Authenticated - mission control and the rest of the dashboard
  return (
    <div className={darkMode ? 'dark' : ''}>
      <RelayDataProvider userKey={currentUser?.id ?? 'api-key'}>
        <AuthenticatedShell
          activeTab={activeTab}
          darkMode={darkMode}
          onToggleDarkMode={handleToggleDarkMode}
          onLogout={handleLogout}
          userName={currentUser?.name || currentUser?.email}
          isAdmin={currentUser?.is_admin === true}
          onSearch={handleSearch}
          onShowShortcuts={() => setShortcutsOpen(true)}
          showNewMemory={showNewMemoryModal}
          onCloseNewMemory={() => setShowNewMemoryModal(false)}
          onTabChange={setActiveTab}
        />
      </RelayDataProvider>

      <CommandPalette
        isOpen={commandPaletteOpen}
        onClose={() => setCommandPaletteOpen(false)}
        onNavigate={(tab) => setActiveTab(tab)}
        isAdmin={currentUser?.is_admin === true}
        onShowShortcuts={() => {
          setCommandPaletteOpen(false);
          setShortcutsOpen(true);
        }}
        onNewMemory={() => {
          setCommandPaletteOpen(false);
          setActiveTab('memories');
          setShowNewMemoryModal(true);
        }}
      />
      <ShortcutsDialog open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
    </div>
  );
}

const RELAY_TABS: TabType[] = ['home', 'trail', 'agents', 'inbox'];

function AuthenticatedShell({
  activeTab,
  darkMode,
  onToggleDarkMode,
  onLogout,
  userName,
  isAdmin,
  onSearch,
  onShowShortcuts,
  showNewMemory,
  onCloseNewMemory,
  onTabChange,
}: {
  activeTab: TabType;
  darkMode: boolean;
  onToggleDarkMode: () => void;
  onLogout: () => void;
  userName?: string;
  isAdmin: boolean;
  onSearch: () => void;
  onShowShortcuts: () => void;
  showNewMemory: boolean;
  onCloseNewMemory: () => void;
  onTabChange: (tab: TabType) => void;
}) {
  const { inbox } = useRelayData();
  const tab: TabType = activeTab === 'admin' && !isAdmin ? 'home' : activeTab;
  return (
    <AppLayout
      activeTab={tab}
      darkMode={darkMode}
      onToggleDarkMode={onToggleDarkMode}
      onLogout={onLogout}
      userName={userName}
      onSearch={onSearch}
      onShowShortcuts={onShowShortcuts}
      isAdmin={isAdmin}
      inboxUnread={inbox.data?.unread_total ?? 0}
    >
      {tab === 'home' && <Home userName={userName} />}
      {tab === 'trail' && <Trail />}
      {tab === 'agents' && <Agents />}
      {tab === 'inbox' && <Inbox />}
      {!RELAY_TABS.includes(tab) && (
        <Dashboard
          activeTab={tab}
          onLogout={onLogout}
          showNewMemory={showNewMemory}
          onCloseNewMemory={onCloseNewMemory}
          onTabChange={onTabChange}
        />
      )}
    </AppLayout>
  );
}

export default App;
