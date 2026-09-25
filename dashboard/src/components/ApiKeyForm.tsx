import { useState } from 'react';
import { Loader2 } from 'lucide-react';
import { api } from '../lib/api';
import { BrandLockup } from '../brand/Brand';

interface ApiKeyFormProps {
  onAuthenticated: () => void;
}

export function ApiKeyForm({ onAuthenticated }: ApiKeyFormProps) {
  const [apiKey, setApiKey] = useState('');
  const [userId, setUserId] = useState(api.getUserId() || '');
  const [projectId, setProjectId] = useState(api.getProjectId() || '');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    
    if (!apiKey.trim()) {
      setError('Please enter an API key');
      return;
    }

    setLoading(true);
    setError(null);

    // Set credentials
    api.setApiKey(apiKey.trim());
    if (userId.trim()) api.setUserId(userId.trim());
    if (projectId.trim()) api.setProjectId(projectId.trim());
    
    try {
      await api.listMemories({ limit: 1 });
      onAuthenticated();
    } catch (err) {
      api.clearAll();
      setError(err instanceof Error ? err.message : 'Invalid API key or credentials');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-paper px-4">
      <div className="max-w-md w-full">
        <div className="text-center mb-8">
          <div className="mb-6 flex items-center justify-center lg:hidden">
            <BrandLockup height={38} className="text-ink" />
          </div>
          <h1 className="font-display text-3xl font-extrabold tracking-[-0.03em] text-ink">
            Sign in with an API key
          </h1>
          <p className="text-gray-500 dark:text-gray-400 mt-2">
            Enter your API key to access your memories
          </p>
        </div>

        <form onSubmit={handleSubmit} className="bg-white dark:bg-gray-800 rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-6">
          <div className="space-y-4">
            <div>
              <label htmlFor="apiKey" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                API Key
              </label>
              <input
                id="apiKey"
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="rem_..."
                className="w-full px-4 py-3 rounded-lg bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-signal focus:border-transparent"
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label htmlFor="userId" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  User ID
                </label>
                <input
                  id="userId"
                  type="text"
                  value={userId}
                  onChange={(e) => setUserId(e.target.value)}
                  placeholder="default_user"
                  className="w-full px-4 py-3 rounded-lg bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-signal focus:border-transparent text-sm"
                />
              </div>
              <div>
                <label htmlFor="projectId" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  Project ID
                </label>
                <input
                  id="projectId"
                  type="text"
                  value={projectId}
                  onChange={(e) => setProjectId(e.target.value)}
                  placeholder="default"
                  className="w-full px-4 py-3 rounded-lg bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-signal focus:border-transparent text-sm"
                />
              </div>
            </div>

            {error && (
              <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full py-3 px-4 rounded-lg bg-accent hover:bg-accent-hover disabled:bg-accent/50 text-white font-medium transition-colors flex items-center justify-center gap-2"
            >
              {loading ? (
                <>
                  <Loader2 className="w-5 h-5 animate-spin" />
                  Connecting...
                </>
              ) : (
                'Connect'
              )}
            </button>
          </div>
        </form>

        <p className="text-center text-xs text-gray-400 mt-6">
          Your API key is stored locally and never sent to third parties.
        </p>
      </div>
    </div>
  );
}
