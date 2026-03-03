import { useState, useEffect, useCallback } from 'react';
import { 
  Key, 
  Plus, 
  Trash2, 
  Copy, 
  Check, 
  X, 
  Loader2, 
  Eye,
  EyeOff,
  Shield,
  Calendar,
  Clock,
  AlertTriangle
} from 'lucide-react';
import clsx from 'clsx';
import { api, type ApiKeyInfo, type CreateApiKeyResponse } from '../lib/api';

// Types - use API types
type ApiKey = ApiKeyInfo;
type CreateKeyResponse = CreateApiKeyResponse;

// Permission badge colors
const PERMISSION_STYLES = {
  admin: 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400',
  editor: 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400',
  viewer: 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300',
};

const PERMISSION_LABELS = {
  admin: 'Admin',
  editor: 'Editor',
  viewer: 'Viewer',
};

export function ApiKeyManager() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  
  // Modal states
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState<ApiKey | null>(null);
  const [newKeyResult, setNewKeyResult] = useState<CreateKeyResponse | null>(null);

  // Fetch keys from API
  const fetchKeys = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listKeys();
      setKeys(response.keys);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load API keys');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchKeys();
  }, [fetchKeys]);

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return 'Never';
    const date = new Date(dateStr);
    return date.toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const formatRelativeDate = (dateStr: string | null) => {
    if (!dateStr) return 'Never used';
    const date = new Date(dateStr);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
    
    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    if (diffDays < 7) return `${diffDays} days ago`;
    if (diffDays < 30) return `${Math.floor(diffDays / 7)} weeks ago`;
    return formatDate(dateStr);
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            API Keys
          </h2>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Manage API keys for accessing your Remembra data
          </p>
        </div>
        <button
          onClick={() => setShowCreateModal(true)}
          className={clsx(
            'px-4 py-2 rounded-lg font-medium',
            'bg-blue-600 hover:bg-blue-700 text-white',
            'flex items-center gap-2 transition-colors'
          )}
        >
          <Plus className="w-4 h-4" />
          Create Key
        </button>
      </div>

      {/* Error */}
      {error && (
        <div className="p-4 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800">
          <p className="text-red-600 dark:text-red-400">{error}</p>
        </div>
      )}

      {/* Loading */}
      {loading && (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
        </div>
      )}

      {/* Keys List */}
      {!loading && keys.length === 0 && (
        <div className="text-center py-12 bg-gray-50 dark:bg-gray-800/50 rounded-lg border border-dashed border-gray-300 dark:border-gray-700">
          <Key className="w-12 h-12 text-gray-400 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-white mb-2">
            No API keys yet
          </h3>
          <p className="text-gray-500 dark:text-gray-400 mb-4">
            Create your first API key to start using Remembra
          </p>
          <button
            onClick={() => setShowCreateModal(true)}
            className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white font-medium"
          >
            Create your first key
          </button>
        </div>
      )}

      {!loading && keys.length > 0 && (
        <div className="space-y-3">
          {keys.map((key) => (
            <div
              key={key.id}
              className={clsx(
                'bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700',
                'p-4 transition-all hover:border-gray-300 dark:hover:border-gray-600'
              )}
            >
              <div className="flex items-start justify-between gap-4">
                {/* Key Info */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-3 mb-2">
                    <h3 className="font-medium text-gray-900 dark:text-white truncate">
                      {key.name}
                    </h3>
                    <span className={clsx(
                      'px-2 py-0.5 rounded text-xs font-medium',
                      PERMISSION_STYLES[key.permission]
                    )}>
                      {PERMISSION_LABELS[key.permission]}
                    </span>
                  </div>
                  
                  {/* Key preview */}
                  <div className="flex items-center gap-2 mb-3">
                    <code className="px-2 py-1 rounded bg-gray-100 dark:bg-gray-900 text-sm font-mono text-gray-600 dark:text-gray-400">
                      rem_{key.key_preview}
                    </code>
                  </div>

                  {/* Metadata */}
                  <div className="flex flex-wrap items-center gap-4 text-sm text-gray-500 dark:text-gray-400">
                    <div className="flex items-center gap-1.5">
                      <Calendar className="w-4 h-4" />
                      <span>Created {formatDate(key.created_at)}</span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Clock className="w-4 h-4" />
                      <span>Last used: {formatRelativeDate(key.last_used_at)}</span>
                    </div>
                  </div>
                </div>

                {/* Actions */}
                <button
                  onClick={() => setShowDeleteModal(key)}
                  className={clsx(
                    'p-2 rounded-lg transition-colors',
                    'text-gray-400 hover:text-red-500',
                    'hover:bg-red-50 dark:hover:bg-red-900/20'
                  )}
                  title="Delete key"
                >
                  <Trash2 className="w-5 h-5" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Create Key Modal */}
      {showCreateModal && (
        <CreateKeyModal
          onClose={() => setShowCreateModal(false)}
          onCreated={(result) => {
            setNewKeyResult(result);
            setShowCreateModal(false);
            fetchKeys();
          }}
        />
      )}

      {/* New Key Result Modal (shows full key once) */}
      {newKeyResult && (
        <NewKeyResultModal
          keyData={newKeyResult}
          onClose={() => setNewKeyResult(null)}
        />
      )}

      {/* Delete Confirmation Modal */}
      {showDeleteModal && (
        <DeleteKeyModal
          keyData={showDeleteModal}
          onClose={() => setShowDeleteModal(null)}
          onDeleted={() => {
            setShowDeleteModal(null);
            fetchKeys();
          }}
        />
      )}
    </div>
  );
}

// Create Key Modal
interface CreateKeyModalProps {
  onClose: () => void;
  onCreated: (result: CreateKeyResponse) => void;
}

function CreateKeyModal({ onClose, onCreated }: CreateKeyModalProps) {
  const [name, setName] = useState('');
  const [permission, setPermission] = useState<'admin' | 'editor' | 'viewer'>('editor');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setError('Please enter a name for this key');
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const result = await api.createKey(name.trim(), permission);
      onCreated(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create API key');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
      <div className="w-full max-w-md bg-white dark:bg-gray-800 rounded-xl shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            Create API Key
          </h2>
          <button
            onClick={onClose}
            className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          {/* Name */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              Key Name
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g., Production App, Development"
              className={clsx(
                'w-full px-4 py-3 rounded-lg border',
                'bg-white dark:bg-gray-900',
                'border-gray-300 dark:border-gray-600',
                'text-gray-900 dark:text-white',
                'placeholder-gray-400 dark:placeholder-gray-500',
                'focus:ring-2 focus:ring-blue-500 focus:border-transparent'
              )}
              disabled={loading}
              autoFocus
            />
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              A descriptive name to identify this key
            </p>
          </div>

          {/* Permission */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              <Shield className="w-4 h-4 inline mr-1" />
              Permission Level
            </label>
            <div className="space-y-2">
              {(['admin', 'editor', 'viewer'] as const).map((perm) => (
                <label
                  key={perm}
                  className={clsx(
                    'flex items-center gap-3 p-3 rounded-lg border cursor-pointer transition-colors',
                    permission === perm
                      ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                      : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'
                  )}
                >
                  <input
                    type="radio"
                    name="permission"
                    value={perm}
                    checked={permission === perm}
                    onChange={() => setPermission(perm)}
                    className="text-blue-600"
                    disabled={loading}
                  />
                  <div>
                    <span className={clsx(
                      'px-2 py-0.5 rounded text-xs font-medium',
                      PERMISSION_STYLES[perm]
                    )}>
                      {PERMISSION_LABELS[perm]}
                    </span>
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      {perm === 'admin' && 'Full access: read, write, delete, manage keys'}
                      {perm === 'editor' && 'Read and write access to memories'}
                      {perm === 'viewer' && 'Read-only access to memories'}
                    </p>
                  </div>
                </label>
              ))}
            </div>
          </div>

          {/* Error */}
          {error && (
            <div className="p-3 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 text-sm">
              {error}
            </div>
          )}

          {/* Actions */}
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className={clsx(
                'px-4 py-2 rounded-lg font-medium',
                'bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600',
                'text-gray-700 dark:text-gray-200'
              )}
              disabled={loading}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading || !name.trim()}
              className={clsx(
                'px-4 py-2 rounded-lg font-medium',
                'bg-blue-600 hover:bg-blue-700 text-white',
                'flex items-center gap-2',
                (loading || !name.trim()) && 'opacity-50 cursor-not-allowed'
              )}
            >
              {loading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Creating...
                </>
              ) : (
                <>
                  <Key className="w-4 h-4" />
                  Create Key
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// New Key Result Modal (shows full key ONCE)
interface NewKeyResultModalProps {
  keyData: CreateKeyResponse;
  onClose: () => void;
}

function NewKeyResultModal({ keyData, onClose }: NewKeyResultModalProps) {
  const [copied, setCopied] = useState(false);
  const [showKey, setShowKey] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(keyData.key);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for older browsers
      const textArea = document.createElement('textarea');
      textArea.value = keyData.key;
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand('copy');
      document.body.removeChild(textArea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
      <div className="w-full max-w-md bg-white dark:bg-gray-800 rounded-xl shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-full bg-green-100 dark:bg-green-900/30 flex items-center justify-center">
              <Check className="w-5 h-5 text-green-600 dark:text-green-400" />
            </div>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
              API Key Created
            </h2>
          </div>
        </div>

        {/* Content */}
        <div className="p-4 space-y-4">
          {/* Warning */}
          <div className="p-3 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800">
            <div className="flex items-start gap-2">
              <AlertTriangle className="w-5 h-5 text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-amber-800 dark:text-amber-300">
                  Copy your API key now!
                </p>
                <p className="text-xs text-amber-700 dark:text-amber-400 mt-1">
                  This is the only time you'll see the full key. Store it securely.
                </p>
              </div>
            </div>
          </div>

          {/* Key Details */}
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-medium text-gray-500 dark:text-gray-400 mb-1">
                Name
              </label>
              <p className="text-gray-900 dark:text-white font-medium">{keyData.name}</p>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-500 dark:text-gray-400 mb-1">
                Permission
              </label>
              <span className={clsx(
                'px-2 py-0.5 rounded text-xs font-medium',
                PERMISSION_STYLES[keyData.permission]
              )}>
                {PERMISSION_LABELS[keyData.permission]}
              </span>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-500 dark:text-gray-400 mb-1">
                API Key
              </label>
              <div className="flex items-center gap-2 w-full">
                <code className={clsx(
                  'flex-1 min-w-0 px-3 py-2 rounded-lg font-mono text-sm',
                  'bg-gray-100 dark:bg-gray-900',
                  'border border-gray-200 dark:border-gray-700',
                  'text-gray-900 dark:text-white',
                  'overflow-hidden text-ellipsis whitespace-nowrap',
                  !showKey && 'tracking-wider'
                )}>
                  {showKey ? keyData.key : '•'.repeat(Math.min(keyData.key.length, 40))}
                </code>
                <div className="flex items-center gap-1 flex-shrink-0">
                  <button
                    onClick={() => setShowKey(!showKey)}
                    className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500 flex-shrink-0"
                    title={showKey ? 'Hide key' : 'Show key'}
                  >
                    {showKey ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                  </button>
                  <button
                    onClick={handleCopy}
                    className={clsx(
                      'p-2 rounded-lg transition-colors flex-shrink-0',
                      copied
                        ? 'bg-green-100 dark:bg-green-900/30 text-green-600 dark:text-green-400'
                        : 'hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500'
                    )}
                    title={copied ? 'Copied!' : 'Copy key'}
                  >
                    {copied ? <Check className="w-5 h-5" /> : <Copy className="w-5 h-5" />}
                  </button>
                </div>
              </div>
            </div>
          </div>

          {/* Actions */}
          <div className="flex justify-end pt-2">
            <button
              onClick={onClose}
              className={clsx(
                'px-4 py-2 rounded-lg font-medium',
                'bg-blue-600 hover:bg-blue-700 text-white'
              )}
            >
              Done
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// Delete Key Confirmation Modal
interface DeleteKeyModalProps {
  keyData: ApiKey;
  onClose: () => void;
  onDeleted: () => void;
}

function DeleteKeyModal({ keyData, onClose, onDeleted }: DeleteKeyModalProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmName, setConfirmName] = useState('');

  const handleDelete = async () => {
    if (confirmName !== keyData.name) {
      setError('Key name does not match');
      return;
    }

    setLoading(true);
    setError(null);

    try {
      await api.revokeKey(keyData.id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete API key');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
      <div className="w-full max-w-md bg-white dark:bg-gray-800 rounded-xl shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-2 text-red-600 dark:text-red-400">
            <AlertTriangle className="w-5 h-5" />
            <h2 className="text-lg font-semibold">
              Delete API Key
            </h2>
          </div>
          <button
            onClick={onClose}
            className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="p-4 space-y-4">
          <p className="text-gray-600 dark:text-gray-300">
            This action cannot be undone. Any applications using this key will immediately lose access.
          </p>

          {/* Key Info */}
          <div className="p-3 rounded-lg bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700">
            <div className="flex items-center gap-2 mb-1">
              <span className="font-medium text-gray-900 dark:text-white">{keyData.name}</span>
              <span className={clsx(
                'px-2 py-0.5 rounded text-xs font-medium',
                PERMISSION_STYLES[keyData.permission]
              )}>
                {PERMISSION_LABELS[keyData.permission]}
              </span>
            </div>
            <code className="text-sm text-gray-500 dark:text-gray-400 font-mono">
              rem_{keyData.key_preview}
            </code>
          </div>

          {/* Confirmation Input */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              Type <span className="font-mono text-red-600 dark:text-red-400">"{keyData.name}"</span> to confirm
            </label>
            <input
              type="text"
              value={confirmName}
              onChange={(e) => setConfirmName(e.target.value)}
              placeholder="Enter key name to confirm"
              className={clsx(
                'w-full px-4 py-3 rounded-lg border',
                'bg-white dark:bg-gray-900',
                'border-gray-300 dark:border-gray-600',
                'text-gray-900 dark:text-white',
                'placeholder-gray-400 dark:placeholder-gray-500',
                'focus:ring-2 focus:ring-red-500 focus:border-transparent'
              )}
              disabled={loading}
            />
          </div>

          {/* Error */}
          {error && (
            <div className="p-3 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 text-sm">
              {error}
            </div>
          )}

          {/* Actions */}
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className={clsx(
                'px-4 py-2 rounded-lg font-medium',
                'bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600',
                'text-gray-700 dark:text-gray-200'
              )}
              disabled={loading}
            >
              Cancel
            </button>
            <button
              onClick={handleDelete}
              disabled={loading || confirmName !== keyData.name}
              className={clsx(
                'px-4 py-2 rounded-lg font-medium',
                'bg-red-600 hover:bg-red-700 text-white',
                'flex items-center gap-2',
                (loading || confirmName !== keyData.name) && 'opacity-50 cursor-not-allowed'
              )}
            >
              {loading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Deleting...
                </>
              ) : (
                <>
                  <Trash2 className="w-4 h-4" />
                  Delete Key
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// API key management uses real backend calls via api.listKeys(), api.createKey(), api.revokeKey()
