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
  AlertTriangle,
  Lock,
  FolderKanban,
  Globe,
} from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import clsx from 'clsx';
import { api, type ApiKeyInfo, type CreateApiKeyResponse, type ProjectScopeOption } from '../lib/api';

type ApiKey = ApiKeyInfo;
type CreateKeyResponse = CreateApiKeyResponse;

const PERMISSION_STYLES = {
  admin: 'bg-red-500/10 border-red-500/20 text-red-600 dark:text-red-400',
  editor: 'bg-purple-500/10 border-purple-500/20 text-purple-700 dark:text-purple-400',
  viewer: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-700 dark:text-emerald-400',
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
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState<ApiKey | null>(null);
  const [newKeyResult, setNewKeyResult] = useState<CreateKeyResponse | null>(null);

  const fetchKeys = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listKeys(true);
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
      month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  };

  const formatRelativeDate = (dateStr: string | null) => {
    if (!dateStr) return 'Never used';
    const date = new Date(dateStr);
    const now = new Date();
    const diffDays = Math.floor((now.getTime() - date.getTime()) / (1000 * 60 * 60 * 24));
    
    if (diffDays < 0) return 'Just now';
    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    if (diffDays < 7) return `${diffDays} days ago`;
    if (diffDays < 30) return `${Math.floor(diffDays / 7)} weeks ago`;
    return formatDate(dateStr);
  };

  return (
    <div className="space-y-8 max-w-5xl">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-[hsl(var(--foreground))] flex items-center gap-2">
            <Lock className="w-5 h-5 text-[hsl(var(--primary))]" />
            API Keys
          </h2>
          <p className="text-[13px] text-[hsl(var(--muted-foreground))] mt-1 font-medium">
            Manage programmatic access and agent tokens for this workspace.
          </p>
        </div>
        <motion.button
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
          onClick={() => setShowCreateModal(true)}
          className={clsx(
            'px-4 py-2 rounded-xl text-sm font-semibold',
            'bg-[linear-gradient(135deg,#8B5CF6,#6366f1)] text-white',
            'flex items-center gap-2 shadow-[0_4px_14px_rgba(139,92,246,0.3)]',
            'hover:shadow-[0_6px_20px_rgba(139,92,246,0.4)] transition-all'
          )}
        >
          <Plus className="w-4 h-4" />
          Generate New Key
        </motion.button>
      </div>

      {error && (
        <div className="p-4 rounded-xl bg-red-500/10 border border-red-500/20 text-red-400 text-sm font-medium">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-20">
          <Loader2 className="w-6 h-6 animate-spin text-purple-500" />
        </div>
      ) : keys.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-24 px-4 text-center rounded-2xl premium-chip border border-dashed border-[hsl(var(--border))/0.95]">
          <div className="w-16 h-16 rounded-full premium-chip flex items-center justify-center mb-4">
            <Key className="w-7 h-7 text-[hsl(var(--muted-foreground))]" />
          </div>
          <h3 className="text-base font-semibold text-[hsl(var(--foreground))] mb-2">No active API keys</h3>
          <p className="text-sm text-[hsl(var(--muted-foreground))] max-w-sm mb-6">
            Generate a secure token to authenticate external tools, agents, and CI/CD pipelines against the Remembra network.
          </p>
          <button
            onClick={() => setShowCreateModal(true)}
            className="btn-ghost px-5 py-2.5 rounded-xl font-medium text-sm"
          >
            Create your first key
          </button>
        </div>
      ) : (
        <div className="grid gap-4">
          <AnimatePresence>
            {keys.map((key, index) => (
              <motion.div
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.3, delay: Math.min(index * 0.05, 0.3) }}
                key={key.id}
                className={clsx(
                  'group dashboard-surface rounded-2xl',
                  'p-5 transition-all duration-300',
                  'hover:shadow-xl hover:shadow-purple-500/5 hover:border-[hsl(var(--primary))/0.26]'
                )}
              >
                <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-3 mb-2.5">
                      <h3 className="text-[15px] font-semibold tracking-tight text-[hsl(var(--foreground))] truncate">
                        {key.name || 'Unnamed Application'}
                      </h3>
                      <span className={clsx(
                        'px-2 py-0.5 rounded-md text-[10px] uppercase tracking-wider font-bold border',
                        PERMISSION_STYLES[key.permission || key.role || 'editor']
                      )}>
                        {PERMISSION_LABELS[key.permission || key.role || 'editor']}
                      </span>
                    </div>
                    
                    <div className="flex items-center gap-3 mb-3">
                      <code className="px-2.5 py-1 rounded-md premium-chip border border-[hsl(var(--border))/0.9] text-[13px] font-mono tracking-wider text-[hsl(var(--foreground))]">
                        rem_{key.key_preview}&bull;&bull;&bull;&bull;&bull;&bull;
                      </code>
                    </div>

                    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-[12px] font-medium text-[hsl(var(--muted-foreground))]">
                      <div className="flex items-center gap-1.5">
                        <Calendar className="w-3.5 h-3.5 opacity-70" />
                        <span>Created {formatDate(key.created_at)}</span>
                      </div>
                      <div className="flex items-center gap-1.5">
                        <Clock className="w-3.5 h-3.5 opacity-70" />
                        <span>Last used {formatRelativeDate(key.last_used_at)}</span>
                      </div>
                      <div className="flex items-center gap-1.5">
                        {(key.project_ids?.length ?? 0) > 0 ? (
                          <FolderKanban className="w-3.5 h-3.5 opacity-70" />
                        ) : (
                          <Globe className="w-3.5 h-3.5 opacity-70" />
                        )}
                        <span>
                          {(key.project_ids?.length ?? 0) > 0
                            ? `Scoped to ${key.project_ids.join(', ')}`
                            : 'All projects'}
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center mt-2 md:mt-0 opacity-100 md:opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={() => setShowDeleteModal(key)}
                      className={clsx(
                        'flex items-center gap-2 px-3 py-2 rounded-xl font-medium text-sm',
                        'bg-red-500/10 text-red-400 hover:bg-red-500 hover:text-white',
                        'border border-red-500/20 hover:border-red-500',
                        'transition-all duration-200'
                      )}
                    >
                      <Trash2 className="w-4 h-4" />
                      Revoke
                    </button>
                  </div>
                </div>
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
      )}

      <AnimatePresence>
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

        {newKeyResult && (
          <NewKeyResultModal
            keyData={newKeyResult}
            onClose={() => setNewKeyResult(null)}
          />
        )}

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
      </AnimatePresence>
    </div>
  );
}

// ---------------------------------------------------------
// MODALS (Vercel Style)
// ---------------------------------------------------------

function ModalOverlay({ children, onClose }: { children: React.ReactNode, onClose?: () => void }) {
  return (
    <motion.div 
      initial={{ opacity: 0 }} 
      animate={{ opacity: 1 }} 
      exit={{ opacity: 0 }} 
      className="modal-backdrop fixed inset-0 z-[100] flex items-center justify-center p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && onClose) onClose();
      }}
    >
      <motion.div 
        initial={{ scale: 0.95, opacity: 0, y: 10 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        exit={{ scale: 0.95, opacity: 0, y: 10 }}
        transition={{ type: "spring", duration: 0.4 }}
        className="modal-surface w-full max-w-md rounded-2xl overflow-hidden relative z-[101]"
      >
        {children}
      </motion.div>
    </motion.div>
  );
}

function CreateKeyModal({ onClose, onCreated }: { onClose: () => void; onCreated: (result: CreateKeyResponse) => void }) {
  const [name, setName] = useState('');
  const [permission, setPermission] = useState<'admin' | 'editor' | 'viewer'>('editor');
  const [scopeMode, setScopeMode] = useState<'all' | 'selected'>('all');
  const [projects, setProjects] = useState<ProjectScopeOption[]>([]);
  const [selectedProjectIds, setSelectedProjectIds] = useState<string[]>([]);
  const [loadingProjects, setLoadingProjects] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let isMounted = true;

    void (async () => {
      try {
        const availableProjects = await api.listProjects();
        if (isMounted) {
          setProjects(availableProjects);
        }
      } catch {
        if (isMounted) {
          setProjects([]);
        }
      } finally {
        if (isMounted) {
          setLoadingProjects(false);
        }
      }
    })();

    return () => {
      isMounted = false;
    };
  }, []);

  const toggleProject = (projectId: string) => {
    setSelectedProjectIds((current) =>
      current.includes(projectId)
        ? current.filter((id) => id !== projectId)
        : [...current, projectId],
    );
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return setError('Please enter a name for this token');
    if (scopeMode === 'selected' && selectedProjectIds.length === 0) {
      return setError('Select at least one project or switch the token scope back to all projects');
    }
    setLoading(true);
    setError(null);
    try {
      const result = await api.createKey(
        name.trim(),
        permission,
        scopeMode === 'selected' ? selectedProjectIds : [],
      );
      onCreated(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create API key');
    } finally {
      setLoading(false);
    }
  };

  return (
    <ModalOverlay onClose={onClose}>
      <div className="flex items-center justify-between px-6 py-4 border-b border-[hsl(var(--border))/0.8] bg-[hsl(var(--card))/0.45]">
        <h2 className="text-[17px] font-semibold tracking-tight text-[hsl(var(--foreground))] flex items-center gap-2">
          <Key className="w-4 h-4 text-[hsl(var(--primary))]" />
          Generate Access Token
        </h2>
        <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-[hsl(var(--muted))/0.82] text-[hsl(var(--muted-foreground))] transition-colors">
          <X className="w-4 h-4" />
        </button>
      </div>

      <form onSubmit={handleSubmit} className="p-6 space-y-6">
        <div>
          <label className="block text-sm font-medium text-[hsl(var(--foreground))] mb-2">Token Name</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. CI/CD Pipeline, Production Bot"
            className="input-premium w-full px-4 py-2.5 rounded-xl text-sm"
            disabled={loading}
            autoFocus
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-[hsl(var(--foreground))] mb-3">
            <Shield className="w-4 h-4 inline mr-1.5 opacity-70" />
            Authorization Scope
          </label>
          <div className="space-y-2">
            {(['admin', 'editor', 'viewer'] as const).map((perm) => (
              <label
                key={perm}
                className={clsx(
                  'flex items-center gap-3 p-3 rounded-xl border cursor-pointer transition-all duration-200',
                  permission === perm
                    ? 'border-purple-500/50 bg-purple-500/10 shadow-[inner_0_0_0_1px_rgba(168,85,247,0.2)]'
                    : 'border-[hsl(var(--border))/0.8] bg-[hsl(var(--card))/0.4] hover:bg-[hsl(var(--muted))/0.55]'
                )}
              >
                <input
                  type="radio"
                  name="permission"
                  value={perm}
                  checked={permission === perm}
                  onChange={() => setPermission(perm)}
                  className="hidden"
                />
                <div className={clsx(
                  "w-4 h-4 rounded-full border flex items-center justify-center flex-shrink-0 transition-colors",
                  permission === perm ? "border-purple-500 bg-purple-500" : "border-gray-600"
                )}>
                  {permission === perm && <div className="w-1.5 h-1.5 bg-white rounded-full" />}
                </div>
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-[hsl(var(--foreground))] text-sm">{PERMISSION_LABELS[perm]}</span>
                  </div>
                  <p className="text-xs text-[hsl(var(--muted-foreground))] mt-0.5">
                    {perm === 'admin' && 'Full root access. Can delete workspaces and manage billing.'}
                    {perm === 'editor' && 'Standard access. Can read, write, and modify memory blocks.'}
                    {perm === 'viewer' && 'Strict read-only access. Cannot create or alter data.'}
                  </p>
                </div>
              </label>
            ))}
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium text-[hsl(var(--foreground))] mb-3">
            <FolderKanban className="w-4 h-4 inline mr-1.5 opacity-70" />
            Project Access
          </label>
          <div className="space-y-2">
            <label
              className={clsx(
                'flex items-center gap-3 p-3 rounded-xl border cursor-pointer transition-all duration-200',
                scopeMode === 'all'
                  ? 'border-purple-500/50 bg-purple-500/10 shadow-[inner_0_0_0_1px_rgba(168,85,247,0.2)]'
                  : 'border-[hsl(var(--border))/0.8] bg-[hsl(var(--card))/0.4] hover:bg-[hsl(var(--muted))/0.55]'
              )}
            >
              <input
                type="radio"
                name="scope-mode"
                value="all"
                checked={scopeMode === 'all'}
                onChange={() => setScopeMode('all')}
                className="hidden"
              />
              <Globe className="w-4 h-4 text-[hsl(var(--muted-foreground))]" />
              <div>
                <div className="font-semibold text-[hsl(var(--foreground))] text-sm">All projects</div>
                <p className="text-xs text-[hsl(var(--muted-foreground))] mt-0.5">
                  This key can access every project the owner can access.
                </p>
              </div>
            </label>

            <label
              className={clsx(
                'flex items-center gap-3 p-3 rounded-xl border cursor-pointer transition-all duration-200',
                scopeMode === 'selected'
                  ? 'border-purple-500/50 bg-purple-500/10 shadow-[inner_0_0_0_1px_rgba(168,85,247,0.2)]'
                  : 'border-[hsl(var(--border))/0.8] bg-[hsl(var(--card))/0.4] hover:bg-[hsl(var(--muted))/0.55]'
              )}
            >
              <input
                type="radio"
                name="scope-mode"
                value="selected"
                checked={scopeMode === 'selected'}
                onChange={() => setScopeMode('selected')}
                className="hidden"
              />
              <FolderKanban className="w-4 h-4 text-[hsl(var(--muted-foreground))]" />
              <div>
                <div className="font-semibold text-[hsl(var(--foreground))] text-sm">Selected projects</div>
                <p className="text-xs text-[hsl(var(--muted-foreground))] mt-0.5">
                  Restrict this key to one or more specific project namespaces.
                </p>
              </div>
            </label>
          </div>

          {scopeMode === 'selected' && (
            <div className="mt-3 rounded-xl border border-[hsl(var(--border))/0.8] bg-[hsl(var(--card))/0.35] p-3 space-y-2">
              {loadingProjects ? (
                <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Loading projects...
                </div>
              ) : projects.length === 0 ? (
                <p className="text-sm text-[hsl(var(--muted-foreground))]">
                  No projects available yet. Create a project first or use all-project scope.
                </p>
              ) : (
                projects.map((project) => (
                  <label
                    key={project.id}
                    className="flex items-start gap-3 rounded-xl border border-[hsl(var(--border))/0.7] bg-[hsl(var(--background))/0.6] px-3 py-2.5 cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      checked={selectedProjectIds.includes(project.project_id)}
                      onChange={() => toggleProject(project.project_id)}
                      className="mt-1 rounded border-[hsl(var(--border))]"
                    />
                    <div className="min-w-0">
                      <div className="text-sm font-semibold text-[hsl(var(--foreground))]">
                        {project.name}
                      </div>
                      <div className="text-xs text-[hsl(var(--muted-foreground))]">
                        Namespace: <code>{project.project_id}</code>
                      </div>
                    </div>
                  </label>
                ))
              )}
            </div>
          )}
        </div>

        {error && <div className="text-red-500 dark:text-red-400 text-sm font-medium">{error}</div>}

        <div className="flex justify-end gap-3 pt-2">
          <button type="button" onClick={onClose} disabled={loading} className="btn-ghost px-4 py-2.5 rounded-xl font-medium text-sm">
            Cancel
          </button>
          <button type="submit" disabled={loading || !name.trim()} className={clsx(
              'btn-primary px-5 py-2.5 rounded-xl font-semibold text-sm',
              (loading || !name.trim()) && 'opacity-50 cursor-not-allowed'
            )}>
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Generate Token'}
          </button>
        </div>
      </form>
    </ModalOverlay>
  );
}

function NewKeyResultModal({ keyData, onClose }: { keyData: CreateKeyResponse; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    await navigator.clipboard.writeText(keyData.key).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <ModalOverlay>
      <div className="p-6">
        <div className="flex justify-center mb-6">
          <div className="w-16 h-16 rounded-full bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center shadow-[0_0_30px_rgba(16,185,129,0.2)]">
            <Check className="w-8 h-8 text-emerald-400" />
          </div>
        </div>
        
        <h2 className="text-xl font-bold text-center text-[hsl(var(--foreground))] mb-2">Token Generated</h2>
        <p className="text-sm text-center text-emerald-400 font-medium mb-6">
          Please copy this token now. You will not be able to see it again.
        </p>

        <div className="mb-4 flex justify-center">
          <span className="inline-flex items-center gap-2 rounded-full border border-[hsl(var(--border))/0.8] bg-[hsl(var(--muted))/0.35] px-3 py-1 text-xs font-medium text-[hsl(var(--muted-foreground))]">
            {(keyData.project_ids?.length ?? 0) > 0 ? <FolderKanban className="w-3.5 h-3.5" /> : <Globe className="w-3.5 h-3.5" />}
            {(keyData.project_ids?.length ?? 0) > 0
              ? `Scoped to ${keyData.project_ids.join(', ')}`
              : 'All projects'}
          </span>
        </div>

        <div className="premium-chip border border-[hsl(var(--border))/0.85] rounded-xl p-1 mb-6 flex items-center">
          <code className="flex-1 px-4 text-[13px] font-mono text-[hsl(var(--foreground))] tracking-wider overflow-x-auto scrollbar-hide py-2">
            {keyData.key}
          </code>
          <button onClick={handleCopy} className={clsx(
              "px-4 py-2 ml-1 rounded-lg text-sm font-semibold transition-all flex items-center gap-2",
              copied ? "bg-emerald-500 text-white" : "btn-ghost text-[hsl(var(--foreground))]"
            )}>
            {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>

        <button onClick={onClose} className="btn-primary w-full py-3 rounded-xl font-semibold text-sm">
          I have securely stored this token
        </button>
      </div>
    </ModalOverlay>
  );
}

function DeleteKeyModal({ keyData, onClose, onDeleted }: { keyData: ApiKey; onClose: () => void; onDeleted: () => void }) {
  const [loading, setLoading] = useState(false);
  const [confirmName, setConfirmName] = useState('');
  
  const handleDelete = async () => {
    if (confirmName !== keyData.name) return;
    setLoading(true);
    try {
      await api.revokeKey(keyData.id, true);
      onDeleted();
    } catch (err) {} finally {
      setLoading(false);
    }
  };

  return (
    <ModalOverlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center gap-3 text-red-400 mb-4">
          <AlertTriangle className="w-6 h-6" />
          <h2 className="text-xl font-bold text-[hsl(var(--foreground))]">Revoke Token</h2>
        </div>
        
        <p className="text-sm text-[hsl(var(--muted-foreground))] mb-6 leading-relaxed">
          This will permanently disable <strong className="text-[hsl(var(--foreground))]">"{keyData.name}"</strong>. 
          Any agents or applications using this token will lose access to Remembra immediately. This action cannot be undone.
        </p>

        <div className="mb-6">
          <label className="block text-xs font-semibold text-[hsl(var(--muted-foreground))] uppercase tracking-wider mb-2">
            Type "{keyData.name}" to confirm
          </label>
          <input
            type="text"
            value={confirmName}
            onChange={(e) => setConfirmName(e.target.value)}
            className="input-premium w-full px-4 py-2.5 rounded-xl text-sm border-red-500/30 focus:border-red-500/50 focus:ring-1 focus:ring-red-500/50"
            autoFocus
          />
        </div>

        <div className="flex justify-end gap-3">
          <button onClick={onClose} className="btn-ghost px-5 py-2.5 rounded-xl font-medium text-sm">
            Cancel
          </button>
          <button 
            onClick={handleDelete} 
            disabled={loading || confirmName !== keyData.name} 
            className="px-5 py-2.5 rounded-xl font-semibold text-sm bg-red-500 text-white hover:bg-red-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
          >
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
            Revoke Permanently
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
