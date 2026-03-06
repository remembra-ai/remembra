import { useState } from 'react';
import { api } from '../lib/api';
import { Plus, Send, X, Clock, Loader2 } from 'lucide-react';
import clsx from 'clsx';

interface StoreMemoryProps {
  onStored?: () => void;
  projectId?: string;
}

const TTL_OPTIONS = [
  { label: 'No expiry', value: '' },
  { label: '1 hour', value: '1h' },
  { label: '24 hours', value: '24h' },
  { label: '7 days', value: '7d' },
  { label: '30 days', value: '30d' },
  { label: '1 year', value: '1y' },
];

export function StoreMemory({ onStored, projectId }: StoreMemoryProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [content, setContent] = useState('');
  const [ttl, setTtl] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!content.trim()) return;

    setLoading(true);
    setError(null);
    setSuccess(false);

    try {
      await api.storeMemory(content.trim(), projectId, ttl || undefined);
      setSuccess(true);
      setContent('');
      setTtl('');
      setTimeout(() => {
        setSuccess(false);
        setIsOpen(false);
        onStored?.();
      }, 1500);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to store memory');
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    setIsOpen(false);
    setContent('');
    setTtl('');
    setError(null);
    setSuccess(false);
  };

  if (!isOpen) {
    return (
      <button
        onClick={() => setIsOpen(true)}
        className={clsx(
          'fixed bottom-6 right-6 p-4 rounded-full shadow-lg',
          'bg-[#8B5CF6] hover:bg-[#7C3AED] text-white',
          'transition-all hover:scale-105',
          'flex items-center gap-2'
        )}
      >
        <Plus className="w-6 h-6" />
        <span className="font-medium">Add Memory</span>
      </button>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
      <div className="w-full max-w-lg bg-white dark:bg-gray-800 rounded-xl shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            Store New Memory
          </h2>
          <button
            onClick={handleClose}
            className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          {/* Content */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              Memory Content
            </label>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="Enter information to remember..."
              rows={5}
              className={clsx(
                'w-full px-4 py-3 rounded-lg border',
                'bg-white dark:bg-gray-900',
                'border-gray-300 dark:border-gray-600',
                'text-gray-900 dark:text-white',
                'placeholder-gray-400 dark:placeholder-gray-500',
                'focus:ring-2 focus:ring-[#8B5CF6] focus:border-transparent',
                'resize-none'
              )}
              disabled={loading}
            />
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Facts will be automatically extracted and indexed
            </p>
          </div>

          {/* TTL Selector */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              <Clock className="w-4 h-4 inline mr-1" />
              Time to Live (optional)
            </label>
            <select
              value={ttl}
              onChange={(e) => setTtl(e.target.value)}
              className={clsx(
                'w-full px-4 py-2 rounded-lg border',
                'bg-white dark:bg-gray-900',
                'border-gray-300 dark:border-gray-600',
                'text-gray-900 dark:text-white',
                'focus:ring-2 focus:ring-[#8B5CF6] focus:border-transparent'
              )}
              disabled={loading}
            >
              {TTL_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>

          {/* Error */}
          {error && (
            <div className="p-3 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 text-sm">
              {error}
            </div>
          )}

          {/* Success */}
          {success && (
            <div className="p-3 rounded-lg bg-green-50 dark:bg-green-900/20 text-green-600 dark:text-green-400 text-sm">
              ✓ Memory stored successfully!
            </div>
          )}

          {/* Actions */}
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={handleClose}
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
              disabled={loading || !content.trim()}
              className={clsx(
                'px-4 py-2 rounded-lg font-medium',
                'bg-[#8B5CF6] hover:bg-[#7C3AED] text-white',
                'flex items-center gap-2',
                (loading || !content.trim()) && 'opacity-50 cursor-not-allowed'
              )}
            >
              {loading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Storing...
                </>
              ) : (
                <>
                  <Send className="w-4 h-4" />
                  Store Memory
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
