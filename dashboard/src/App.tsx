import { useState, useEffect } from 'react';
import { Header } from './components/Header';
import { ApiKeyForm } from './components/ApiKeyForm';
import { Dashboard } from './pages/Dashboard';
import { Login } from './pages/Login';
import { Signup } from './pages/Signup';
import { ForgotPassword } from './pages/ForgotPassword';
import { api } from './lib/api';

type AuthMode = 'login' | 'signup' | 'forgot-password' | 'api-key';

function App() {
  const [darkMode, setDarkMode] = useState(() => {
    const saved = localStorage.getItem('darkMode');
    if (saved !== null) return saved === 'true';
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  });
  
  const [isAuthenticated, setIsAuthenticated] = useState(() => {
    // Check for JWT token first, then API key
    return !!localStorage.getItem('remembra_jwt_token') || !!api.getApiKey();
  });
  
  const [authMode, setAuthMode] = useState<AuthMode>('login');
  const [currentUser, setCurrentUser] = useState<{ id: string; email: string; name?: string } | null>(() => {
    const saved = localStorage.getItem('remembra_user');
    return saved ? JSON.parse(saved) : null;
  });

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
        const response = await fetch('/api/v1/auth/me', {
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

  const handleLogin = (token: string, user: { id: string; email: string; name?: string }) => {
    localStorage.setItem('remembra_jwt_token', token);
    localStorage.setItem('remembra_user', JSON.stringify(user));
    // Set user ID in API client for compatibility
    api.setUserId(user.id);
    setCurrentUser(user);
    setIsAuthenticated(true);
  };

  const handleSignup = (user: { id: string; email: string; name?: string }) => {
    // After signup, switch to login
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
        await fetch('/api/v1/auth/logout', {
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
        {authMode === 'api-key' && (
          <ApiKeyForm onAuthenticated={handleApiKeyAuth} />
        )}
        
        {/* Toggle between user auth and API key auth */}
        {authMode !== 'api-key' && (
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

  // Authenticated - show dashboard
  return (
    <div className={darkMode ? 'dark' : ''}>
      <div className="min-h-screen bg-gray-50 dark:bg-gray-900">
        <Header
          darkMode={darkMode}
          onToggleDarkMode={handleToggleDarkMode}
          isAuthenticated={isAuthenticated}
          onLogout={handleLogout}
          userName={currentUser?.name || currentUser?.email}
        />
        <main>
          <Dashboard />
        </main>
      </div>
    </div>
  );
}

export default App;
