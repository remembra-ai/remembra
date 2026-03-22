import { useState, useEffect, useCallback } from 'react';
import { AppLayout } from './components/AppLayout';
import { ApiKeyForm } from './components/ApiKeyForm';
import { CommandPalette } from './components/CommandPalette';
import { Dashboard, type TabType } from './pages/Dashboard';
import { Login } from './pages/Login';
import { Signup } from './pages/Signup';
import { ForgotPassword } from './pages/ForgotPassword';
import { InviteAccept } from './pages/InviteAccept';
import { api } from './lib/api';
import { API_V1 } from './config';

type AuthMode = 'login' | 'signup' | 'forgot-password' | 'reset-password' | 'api-key' | 'invite';

function App() {
  const [darkMode, setDarkMode] = useState(() => {
    const saved = localStorage.getItem('darkMode');
    if (saved !== null) return saved === 'true';
    // Default to dark mode to match Remembra brand
    return true;
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
  const [currentUser, setCurrentUser] = useState<{ id: string; email: string; name?: string } | null>(() => {
    const saved = localStorage.getItem('remembra_user');
    return saved ? JSON.parse(saved) : null;
  });

  const [activeTab, setActiveTab] = useState<TabType>(() => {
    const saved = localStorage.getItem('activeTab');
    return (saved as TabType) || 'memories';
  });

  // Save active tab
  useEffect(() => {
    localStorage.setItem('activeTab', activeTab);
  }, [activeTab]);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', darkMode);
    localStorage.setItem('darkMode', String(darkMode));
  }, [darkMode]);

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
          handleLogout();
        } else {
          const user = await response.json();
          setCurrentUser({ id: user.id, email: user.email, name: user.name });
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
    setDarkMode(!darkMode);
  };

  const handleLogin = async (token: string, user: { id: string; email: string; name?: string }) => {
    localStorage.setItem('remembra_jwt_token', token);
    localStorage.setItem('remembra_user', JSON.stringify(user));
    // Set user ID in API client for compatibility
    api.setUserId(user.id);
    api.setJwtToken(token);
    setCurrentUser(user);
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
          setActiveTab('teams');
          // Clear URL
          window.history.replaceState({}, '', '/');
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

  // Command palette state
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);
  const [showNewMemoryModal, setShowNewMemoryModal] = useState(false);

  const handleSearch = useCallback(() => {
    setCommandPaletteOpen(true);
  }, []);

  const handleNewMemory = useCallback(() => {
    setShowNewMemoryModal(true);
  }, []);

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
            onAccepted={(teamId, teamName) => {
              setActiveTab('teams');
              window.history.replaceState({}, '', '/');
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
          onAccepted={(teamId, teamName) => {
            setInviteToken(null);
            setActiveTab('teams');
            window.history.replaceState({}, '', '/');
          }}
          onSwitchToLogin={() => {}}
          onSwitchToSignup={() => {}}
        />
      </div>
    );
  }

  // Authenticated - show dashboard with new layout
  return (
    <div className={darkMode ? 'dark' : ''}>
      <AppLayout
        activeTab={activeTab}
        onTabChange={setActiveTab}
        darkMode={darkMode}
        onToggleDarkMode={handleToggleDarkMode}
        isAuthenticated={isAuthenticated}
        onLogout={handleLogout}
        userName={currentUser?.name || currentUser?.email}
        onNewMemory={handleNewMemory}
        onSearch={handleSearch}
      >
        <Dashboard
          activeTab={activeTab}
          onLogout={handleLogout}
          showNewMemory={showNewMemoryModal}
          onCloseNewMemory={() => setShowNewMemoryModal(false)}
        />
      </AppLayout>

      {/* Command Palette (⌘K) */}
      <CommandPalette
        isOpen={commandPaletteOpen}
        onClose={() => setCommandPaletteOpen(false)}
        onNavigate={(tab) => setActiveTab(tab)}
        onNewMemory={() => {
          setCommandPaletteOpen(false);
          setShowNewMemoryModal(true);
        }}
      />
    </div>
  );
}

export default App;
