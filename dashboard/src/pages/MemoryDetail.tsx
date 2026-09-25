import { useState } from 'react';
import { api } from '../lib/api';
import type { Memory } from '../lib/api';
import {
  ArrowLeft,
  Calendar,
  Clock,
  User,
  Tag,
  Brain,
  Trash2,
  Loader2,
  AlertTriangle,
  FolderOpen,
  Shield,
} from 'lucide-react';

interface MemoryDetailProps {
  memory: Memory;
  onClose: () => void;
  onDelete?: () => void;
}

export function MemoryDetail({ memory, onClose, onDelete }: MemoryDetailProps) {
  const [deleting, setDeleting] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr);
    return date.toLocaleString('en-US', {
      weekday: 'long',
      month: 'long',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  };

  const handleDelete = async () => {
    try {
      setDeleting(true);
      setError(null);
      await api.deleteMemory(memory.id);
      onDelete?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete memory');
      setShowDeleteConfirm(false);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <button
          onClick={onClose}
          className="flex items-center gap-2 text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white transition-colors"
        >
          <ArrowLeft className="w-5 h-5" />
          <span>Back to memories</span>
        </button>

        <button
          onClick={() => setShowDeleteConfirm(true)}
          className="p-2 rounded-lg text-gray-500 hover:text-red-600 dark:text-gray-400 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
          title="Delete memory"
        >
          <Trash2 className="w-5 h-5" />
        </button>
      </div>

      {/* Delete Confirmation */}
      {showDeleteConfirm && (
        <div className="mb-6 p-4 bg-red-50 dark:bg-red-900/20 rounded-lg border border-red-200 dark:border-red-800">
          <div className="flex items-start gap-3">
            <AlertTriangle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
            <div className="flex-1">
              <h3 className="font-medium text-red-800 dark:text-red-300">
                Delete this memory?
              </h3>
              <p className="text-sm text-red-700 dark:text-red-400 mt-1">
                This action cannot be undone. The memory will be permanently deleted.
              </p>
              <div className="flex gap-3 mt-4">
                <button
                  onClick={handleDelete}
                  disabled={deleting}
                  className="px-4 py-2 rounded-lg bg-red-600 hover:bg-red-700 disabled:bg-red-400 text-white text-sm font-medium transition-colors flex items-center gap-2"
                >
                  {deleting ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" />
                      Deleting...
                    </>
                  ) : (
                    'Yes, delete'
                  )}
                </button>
                <button
                  onClick={() => setShowDeleteConfirm(false)}
                  className="px-4 py-2 rounded-lg bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-300 text-sm font-medium transition-colors"
                >
                  Cancel
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div className="mb-6 p-4 bg-red-50 dark:bg-red-900/20 rounded-lg border border-red-200 dark:border-red-800">
          <p className="text-red-600 dark:text-red-400">{error}</p>
        </div>
      )}

      {/* Content */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
        <div className="p-6">
          <p className="text-gray-900 dark:text-gray-100 text-lg leading-relaxed whitespace-pre-wrap">
            {memory.content}
          </p>
        </div>

        {/* Metadata */}
        <div className="border-t border-gray-200 dark:border-gray-700 p-6 bg-gray-50 dark:bg-gray-800/50">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {/* Created */}
            <div className="flex items-start gap-3">
              <Calendar className="w-5 h-5 text-gray-400 flex-shrink-0 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-gray-500 dark:text-gray-400">Created</p>
                <p className="text-gray-900 dark:text-gray-100">
                  {formatDate(memory.created_at)}
                </p>
              </div>
            </div>

            {/* Last Accessed */}
            {memory.accessed_at && (
              <div className="flex items-start gap-3">
                <Clock className="w-5 h-5 text-gray-400 flex-shrink-0 mt-0.5" />
                <div>
                  <p className="text-sm font-medium text-gray-500 dark:text-gray-400">Last Accessed</p>
                  <p className="text-gray-900 dark:text-gray-100">
                    {formatDate(memory.accessed_at)}
                  </p>
                </div>
              </div>
            )}

            {/* Project */}
            <div className="flex items-start gap-3">
              <FolderOpen className="w-5 h-5 text-gray-400 flex-shrink-0 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-gray-500 dark:text-gray-400">Project</p>
                <p className="text-gray-900 dark:text-gray-100">
                  {memory.project_id || 'default'}
                </p>
              </div>
            </div>

            {/* Access Count */}
            {memory.access_count !== undefined && (
              <div className="flex items-start gap-3">
                <Brain className="w-5 h-5 text-gray-400 flex-shrink-0 mt-0.5" />
                <div>
                  <p className="text-sm font-medium text-gray-500 dark:text-gray-400">Recall Count</p>
                  <p className="text-gray-900 dark:text-gray-100">
                    {memory.access_count} time{memory.access_count !== 1 ? 's' : ''}
                  </p>
                </div>
              </div>
            )}

            {/* Memory Type */}
            {memory.memory_type && (
              <div className="flex items-start gap-3">
                <Tag className="w-5 h-5 text-gray-400 flex-shrink-0 mt-0.5" />
                <div>
                  <p className="text-sm font-medium text-gray-500 dark:text-gray-400">Type</p>
                  <p className="text-gray-900 dark:text-gray-100">{memory.memory_type}</p>
                </div>
              </div>
            )}

            {/* TTL */}
            {memory.ttl && (
              <div className="flex items-start gap-3">
                <Shield className="w-5 h-5 text-gray-400 flex-shrink-0 mt-0.5" />
                <div>
                  <p className="text-sm font-medium text-gray-500 dark:text-gray-400">Time to Live</p>
                  <p className="text-gray-900 dark:text-gray-100">{memory.ttl}</p>
                </div>
              </div>
            )}
          </div>

          {/* Entities */}
          {memory.entities && memory.entities.length > 0 && (
            <div className="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700">
              <div className="flex items-center gap-2 mb-3">
                <User className="w-5 h-5 text-gray-400" />
                <p className="text-sm font-medium text-gray-500 dark:text-gray-400">Entities</p>
              </div>
              <div className="flex flex-wrap gap-2">
                {memory.entities.map((entity, index) => (
                  <span
                    key={index}
                    className="px-3 py-1 rounded-full bg-blue-100 dark:bg-blue-900/30 text-signal-ink dark:text-blue-300 text-sm"
                  >
                    {entity}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Memory ID */}
          <div className="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700">
            <p className="text-xs text-gray-400 font-mono">
              ID: {memory.id}
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
