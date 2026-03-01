import { useState } from 'react';
import { api } from '../lib/api';
import type { Memory } from '../lib/api';
import { Save, X, Loader2, AlertTriangle } from 'lucide-react';
import clsx from 'clsx';

interface EditMemoryProps {
  memory: Memory;
  onSave: (updated: Memory) => void;
  onCancel: () => void;
}

export function EditMemory({ memory, onSave, onCancel }: EditMemoryProps) {
  const [content, setContent] = useState(memory.content);
  const [loading, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = async () => {
    if (!content.trim()) {
      setError('Content cannot be empty');
      return;
    }

    setSaving(true);
    setError(null);

    try {
      const updated = await api.updateMemory(memory.id, content.trim());
      onSave(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update memory');
    } finally {
      setSaving(false);
    }
  };

  const hasChanges = content !== memory.content;

  return (
    <div className="space-y-4">
      {/* Editor */}
      <div>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
          Edit Memory Content
        </label>
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          rows={6}
          className={clsx(
            'w-full px-4 py-3 rounded-lg border',
            'bg-white dark:bg-gray-900',
            'border-gray-300 dark:border-gray-600',
            'text-gray-900 dark:text-white',
            'focus:ring-2 focus:ring-blue-500 focus:border-transparent',
            'resize-none font-mono text-sm'
          )}
          disabled={loading}
        />
        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
          Note: Editing will re-extract facts and update embeddings
        </p>
      </div>

      {/* Warning */}
      {hasChanges && (
        <div className="flex items-start gap-2 p-3 bg-yellow-50 dark:bg-yellow-900/20 rounded-lg">
          <AlertTriangle className="w-5 h-5 text-yellow-600 dark:text-yellow-400 flex-shrink-0 mt-0.5" />
          <div className="text-sm text-yellow-700 dark:text-yellow-300">
            <strong>Changes detected.</strong> Saving will re-process this memory,
            which may update extracted facts and entity links.
          </div>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="p-3 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 text-sm">
          {error}
        </div>
      )}

      {/* Actions */}
      <div className="flex justify-end gap-3">
        <button
          type="button"
          onClick={onCancel}
          className={clsx(
            'px-4 py-2 rounded-lg font-medium',
            'bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600',
            'text-gray-700 dark:text-gray-200'
          )}
          disabled={loading}
        >
          <X className="w-4 h-4 inline mr-2" />
          Cancel
        </button>
        <button
          onClick={handleSave}
          disabled={loading || !hasChanges}
          className={clsx(
            'px-4 py-2 rounded-lg font-medium',
            'bg-blue-600 hover:bg-blue-700 text-white',
            'flex items-center gap-2',
            (loading || !hasChanges) && 'opacity-50 cursor-not-allowed'
          )}
        >
          {loading ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" />
              Saving...
            </>
          ) : (
            <>
              <Save className="w-4 h-4" />
              Save Changes
            </>
          )}
        </button>
      </div>
    </div>
  );
}
