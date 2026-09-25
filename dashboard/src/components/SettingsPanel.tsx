import { useState, useEffect } from 'react';
import { 
  X, 
  Settings as SettingsIcon, 
  Key, 
  User, 
  Database, 
  Wifi, 
  WifiOff, 
  Copy, 
  Check,
  LogOut,
  Globe
} from 'lucide-react';
import { api, type ConnectionStatus } from '../lib/api';

interface SettingsPanelProps {
  isOpen: boolean;
  onClose: () => void;
  onLogout: () => void;
  onOpenApiKeys?: () => void;
}

export function SettingsPanel({ isOpen, onClose, onLogout, onOpenApiKeys }: SettingsPanelProps) {
  const [apiUrl, setApiUrl] = useState('');
  const [projectId, setProjectId] = useState('');
  const [userId, setUserId] = useState('');
  const [connection, setConnection] = useState<ConnectionStatus | null>(null);
  const [apiKey, setApiKey] = useState('');
  const [apiKeyPreview, setApiKeyPreview] = useState('');
  const [copiedField, setCopiedField] = useState<string | null>(null);
  const [showApiKey, setShowApiKey] = useState(false);
  const [checkingConnection, setCheckingConnection] = useState(false);

  useEffect(() => {
    if (isOpen) {
      loadSettings();
      void refreshConnection();
    }
  }, [isOpen]);

  // Escape closes the panel.
  useEffect(() => {
    if (!isOpen) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isOpen, onClose]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const syncProject = () => {
      setProjectId(api.getProjectId() || 'default');
    };

    const handleProjectChanged = (event: Event) => {
      const customEvent = event as CustomEvent<{ projectId?: string }>;
      setProjectId(customEvent.detail?.projectId || api.getProjectId() || 'default');
    };

    window.addEventListener('storage', syncProject);
    window.addEventListener('remembra:project-changed', handleProjectChanged as EventListener);

    return () => {
      window.removeEventListener('storage', syncProject);
      window.removeEventListener('remembra:project-changed', handleProjectChanged as EventListener);
    };
  }, [isOpen]);

  const loadSettings = () => {
    setApiUrl(api.getApiBaseUrl());

    setProjectId(api.getProjectId() || 'default');
    setUserId(api.getUserId());

    const key = api.getApiKey();
    if (key) {
      setApiKeyPreview(`${key.substring(0, 8)}...${key.substring(key.length - 4)}`);
      setApiKey(key);
    } else {
      const token = api.getJwtToken();
      if (token) {
        setApiKeyPreview('JWT Token (via login)');
        setApiKey('');
      } else {
        setApiKeyPreview('');
        setApiKey('');
      }
    }
  };

  const refreshConnection = async () => {
    setCheckingConnection(true);
    try {
      const status = await api.getConnectionStatus();
      setConnection(status);
    } finally {
      setCheckingConnection(false);
    }
  };

  const copyToClipboard = async (text: string, field: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedField(field);
      setTimeout(() => setCopiedField(null), 2000);
    } catch (error) {
      console.error('Failed to copy:', error);
    }
  };

  const handleApiKeyUpdate = () => {
    if (apiKey.trim()) {
      api.setApiKey(apiKey.trim());
      setApiKeyPreview(`${apiKey.substring(0, 8)}...${apiKey.substring(apiKey.length - 4)}`);
      setShowApiKey(false);
      void refreshConnection();
    }
  };

  const handleDisconnect = () => {
    if (confirm('Are you sure you want to disconnect? You will need to sign in again.')) {
      onLogout();
      onClose();
    }
  };

  if (!isOpen) return null;

  const statusTone = checkingConnection
    ? 'checking'
    : !connection || !connection.apiReachable
      ? 'critical'
      : connection.authenticated
        ? 'healthy'
        : 'warning';

  return (
    <>
      {/* Backdrop */}
      <div 
        className="modal-backdrop fixed inset-0 z-50"
        onClick={onClose}
      />

      {/* Panel */}
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="connection-panel-title"
        className="fixed inset-y-0 right-0 w-full max-w-md dashboard-surface border-l border-[hsl(var(--border))/0.72] z-50 overflow-y-auto"
      >
        {/* Header */}
        <div className="sticky top-0 dashboard-surface border-b border-[hsl(var(--border))/0.72] px-6 py-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-[hsl(var(--primary))]/10">
                <SettingsIcon className="w-5 h-5 text-[hsl(var(--primary))]" />
              </div>
              <div>
                <h2 id="connection-panel-title" className="font-display text-lg font-bold text-[hsl(var(--foreground))]">
                  Connection
                </h2>
                <p className="text-xs text-[hsl(var(--muted-foreground))]">
                  Server, project and credentials for your agents
                </p>
              </div>
            </div>
            <button
              onClick={onClose}
              aria-label="Close"
              className="p-2 rounded-lg hover:bg-[hsl(var(--muted))/0.78] text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="p-6 space-y-6">
          {/* Connection Status */}
          <div className="premium-chip rounded-2xl p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold text-[hsl(var(--foreground))]">
                Connection Status
              </h3>
              {statusTone === 'checking' ? (
                <div className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]">
                  <span className="h-2 w-2 rounded-full bg-[hsl(var(--muted-foreground))] animate-pulse" />
                  Checking
                </div>
              ) : statusTone === 'healthy' ? (
                <div className="flex items-center gap-2 text-xs text-emerald-400">
                  <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
                  Connected
                </div>
              ) : statusTone === 'warning' ? (
                <div className="flex items-center gap-2 text-xs text-amber-500">
                  <span className="h-2 w-2 rounded-full bg-amber-500 animate-pulse" />
                  Auth Required
                </div>
              ) : (
                <div className="flex items-center gap-2 text-xs text-red-400">
                  <WifiOff className="w-3 h-3" />
                  Disconnected
                </div>
              )}
            </div>
            <div className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]">
              {statusTone === 'checking' ? (
                <>
                  <Wifi className="w-3.5 h-3.5" />
                  Verifying server and credentials
                </>
              ) : statusTone === 'healthy' ? (
                <>
                  <Wifi className="w-3.5 h-3.5 text-emerald-400" />
                  {connection?.detail || 'API responding normally'}
                </>
              ) : statusTone === 'warning' ? (
                <>
                  <Wifi className="w-3.5 h-3.5 text-amber-500" />
                  {connection?.detail || 'API online, but authentication is incomplete'}
                </>
              ) : (
                <>
                  <WifiOff className="w-3.5 h-3.5" />
                  {connection?.detail || 'Cannot reach API server'}
                </>
              )}
            </div>
          </div>

          {connection?.serverVersion && (
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              Server version: <span className="font-mono text-[hsl(var(--foreground))]">{connection.serverVersion}</span>
            </p>
          )}

          {/* API URL */}
          <div>
            <label className="block text-sm font-medium text-[hsl(var(--foreground))] mb-2">
              <Globe className="w-4 h-4 inline mr-2" />
              API URL
            </label>
            <div className="flex gap-2">
              <input
                type="text"
                value={apiUrl}
                readOnly
                className="flex-1 px-3 py-2 rounded-lg premium-chip text-sm text-[hsl(var(--foreground))] font-mono"
              />
              <button
                onClick={() => copyToClipboard(apiUrl, 'apiUrl')}
                className="p-2 rounded-lg premium-chip hover:bg-[hsl(var(--muted))/0.82] text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
                title="Copy"
              >
                {copiedField === 'apiUrl' ? (
                  <Check className="w-4 h-4 text-emerald-400" />
                ) : (
                  <Copy className="w-4 h-4" />
                )}
              </button>
            </div>
          </div>

          {/* Project ID */}
          <div>
            <label className="block text-sm font-medium text-[hsl(var(--foreground))] mb-2">
              <Database className="w-4 h-4 inline mr-2" />
              Current Project ID
            </label>
            <div className="flex gap-2">
              <input
                type="text"
                value={projectId}
                readOnly
                className="flex-1 px-3 py-2 rounded-lg premium-chip text-sm text-[hsl(var(--foreground))] font-mono"
              />
              <button
                onClick={() => copyToClipboard(projectId, 'projectId')}
                className="p-2 rounded-lg premium-chip hover:bg-[hsl(var(--muted))/0.82] text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
                title="Copy"
              >
                {copiedField === 'projectId' ? (
                  <Check className="w-4 h-4 text-emerald-400" />
                ) : (
                  <Copy className="w-4 h-4" />
                )}
              </button>
            </div>
            <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">
              Use the project switcher in the header to change projects
            </p>
          </div>

          {/* User ID */}
          <div>
            <label className="block text-sm font-medium text-[hsl(var(--foreground))] mb-2">
              <User className="w-4 h-4 inline mr-2" />
              User ID
            </label>
            <div className="flex gap-2">
              <input
                type="text"
                value={userId}
                readOnly
                className="flex-1 px-3 py-2 rounded-lg premium-chip text-sm text-[hsl(var(--foreground))] font-mono"
              />
              <button
                onClick={() => copyToClipboard(userId, 'userId')}
                className="p-2 rounded-lg premium-chip hover:bg-[hsl(var(--muted))/0.82] text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
                title="Copy"
              >
                {copiedField === 'userId' ? (
                  <Check className="w-4 h-4 text-emerald-400" />
                ) : (
                  <Copy className="w-4 h-4" />
                )}
              </button>
            </div>
          </div>

          {/* API Key Management */}
          <div>
            <label className="block text-sm font-medium text-[hsl(var(--foreground))] mb-2">
              <Key className="w-4 h-4 inline mr-2" />
              API Key
            </label>
            {!showApiKey ? (
              <div className="space-y-2">
                <div className="flex gap-2">
                  <input
                    type="password"
                    value={apiKeyPreview}
                    readOnly
                    className="flex-1 px-3 py-2 rounded-lg premium-chip text-sm text-[hsl(var(--muted-foreground))] font-mono"
                  />
                  <button
                    onClick={() => setShowApiKey(true)}
                    className="px-3 py-2 rounded-lg premium-chip hover:bg-[hsl(var(--muted))/0.82] text-[hsl(var(--foreground))] text-sm transition-colors"
                  >
                    Edit
                  </button>
                </div>
                <p className="text-xs text-[hsl(var(--muted-foreground))]">
                  {connection?.authMode === 'jwt'
                    ? 'Authenticated via JWT token'
                    : connection?.authMode === 'api_key'
                      ? 'Authenticated via API key'
                      : 'No credentials loaded'}
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                <input
                  type="text"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="rem_..."
                  className="w-full px-3 py-2 rounded-lg premium-chip text-sm text-[hsl(var(--foreground))] font-mono focus:ring-2 focus:ring-[hsl(var(--primary))]"
                />
                <div className="flex gap-2">
                  <button
                    onClick={handleApiKeyUpdate}
                    className="flex-1 px-3 py-2 rounded-lg bg-[hsl(var(--primary))] hover:bg-[hsl(var(--primary))]/90 text-white text-sm font-medium transition-colors"
                  >
                    Save
                  </button>
                  <button
                    onClick={() => {
                      setShowApiKey(false);
                      setApiKey(api.getApiKey() || '');
                    }}
                    className="flex-1 px-3 py-2 rounded-lg premium-chip hover:bg-[hsl(var(--muted))/0.82] text-[hsl(var(--foreground))] text-sm transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>

          {/* Actions */}
          <div className="pt-4 border-t border-[hsl(var(--border))/0.72] space-y-2">
            <button
              onClick={handleDisconnect}
              className="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg border border-red-500/30 bg-red-500/10 hover:bg-red-500/20 text-red-500 font-medium transition-colors"
            >
              <LogOut className="w-4 h-4" />
              Disconnect / Switch Account
            </button>
          </div>

          {/* Info */}
          <div className="premium-chip rounded-2xl p-4">
            <h4 className="text-xs font-semibold text-[hsl(var(--foreground))] mb-2">
              Need Help?
            </h4>
            <p className="text-xs text-[hsl(var(--muted-foreground))] leading-relaxed">
              To manage API keys or change your account settings, visit the{' '}
              <button
                onClick={() => {
                  onClose();
                  onOpenApiKeys?.();
                }}
                className="text-[hsl(var(--primary))] hover:underline"
              >
                API Keys
              </button>{' '}
              tab.
            </p>
          </div>
        </div>
      </div>
    </>
  );
}
