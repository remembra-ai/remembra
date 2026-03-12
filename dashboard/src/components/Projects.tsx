import { useState, useEffect } from 'react';
import { 
  FolderOpen, 
  Plus, 
  Settings, 
  Trash2, 
  Users, 
  Lock, 
  Globe,
  MoreVertical,
  Search,
  Loader2,
  FolderPlus,
  X
} from 'lucide-react';
import clsx from 'clsx';
import { API_V1 } from '../config';

interface Space {
  id: string;
  name: string;
  description: string;
  owner_id: string;
  project_id: string;
  created_at: string;
  updated_at?: string;
  members: number;
  memory_count?: number;
}

interface SpaceMember {
  agent_id: string;
  permission: string;
  granted_by: string;
  granted_at: string;
}

export function Projects() {
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [selectedSpace, setSelectedSpace] = useState<Space | null>(null);
  const [members, setMembers] = useState<SpaceMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  
  // Modals
  const [showCreateSpace, setShowCreateSpace] = useState(false);
  const [showGrantAccess, setShowGrantAccess] = useState(false);
  
  // Form states
  const [newSpaceName, setNewSpaceName] = useState('');
  const [newSpaceDescription, setNewSpaceDescription] = useState('');
  const [grantAgentId, setGrantAgentId] = useState('');
  const [grantPermission, setGrantPermission] = useState<'read' | 'write' | 'admin'>('read');
  const [formLoading, setFormLoading] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const getAuthHeaders = () => {
    const token = localStorage.getItem('remembra_jwt_token');
    if (token) {
      return { 'Authorization': `Bearer ${token}` };
    }
    const apiKey = localStorage.getItem('remembra_api_key');
    if (apiKey) {
      return { 'X-Api-Key': apiKey };
    }
    return {};
  };

  // Fetch spaces
  useEffect(() => {
    fetchSpaces();
  }, []);

  // Fetch members when space is selected
  useEffect(() => {
    if (selectedSpace) {
      fetchMembers(selectedSpace.id);
    }
  }, [selectedSpace]);

  const fetchSpaces = async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_V1}/spaces`, {
        headers: getAuthHeaders(),
      });
      
      if (!response.ok) {
        if (response.status === 503) {
          // Spaces not enabled
          setError('Projects feature is not enabled on this instance.');
          setSpaces([]);
          return;
        }
        throw new Error('Failed to fetch projects');
      }
      
      const data = await response.json();
      setSpaces(data.spaces || []);
      
      // Auto-select first space if available
      if (data.spaces?.length > 0 && !selectedSpace) {
        setSelectedSpace(data.spaces[0]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load projects');
    } finally {
      setLoading(false);
    }
  };

  const fetchMembers = async (spaceId: string) => {
    try {
      const response = await fetch(`${API_V1}/spaces/${spaceId}/access`, {
        headers: getAuthHeaders(),
      });
      
      if (!response.ok) {
        return;
      }
      
      const data = await response.json();
      setMembers(data.access || []);
    } catch (err) {
      console.error('Failed to fetch members:', err);
    }
  };

  const createSpace = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormLoading(true);
    setFormError(null);

    try {
      const response = await fetch(`${API_V1}/spaces`, {
        method: 'POST',
        headers: {
          ...getAuthHeaders(),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          name: newSpaceName,
          description: newSpaceDescription,
        }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to create project');
      }

      const newSpace = await response.json();
      setSpaces([...spaces, newSpace]);
      setSelectedSpace(newSpace);
      setShowCreateSpace(false);
      setNewSpaceName('');
      setNewSpaceDescription('');
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to create project');
    } finally {
      setFormLoading(false);
    }
  };

  const deleteSpace = async (spaceId: string) => {
    if (!confirm('Are you sure you want to delete this project? All memories in it will be unlinked.')) {
      return;
    }

    try {
      const response = await fetch(`${API_V1}/spaces/${spaceId}`, {
        method: 'DELETE',
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        throw new Error('Failed to delete project');
      }

      setSpaces(spaces.filter(s => s.id !== spaceId));
      if (selectedSpace?.id === spaceId) {
        setSelectedSpace(spaces.length > 1 ? spaces[0] : null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete project');
    }
  };

  const grantAccess = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedSpace) return;
    
    setFormLoading(true);
    setFormError(null);

    try {
      const response = await fetch(`${API_V1}/spaces/${selectedSpace.id}/access`, {
        method: 'POST',
        headers: {
          ...getAuthHeaders(),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          agent_id: grantAgentId,
          permission: grantPermission,
        }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to grant access');
      }

      await fetchMembers(selectedSpace.id);
      setShowGrantAccess(false);
      setGrantAgentId('');
      setGrantPermission('read');
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to grant access');
    } finally {
      setFormLoading(false);
    }
  };

  const revokeAccess = async (agentId: string) => {
    if (!selectedSpace) return;
    
    if (!confirm(`Revoke access for ${agentId}?`)) return;
    
    try {
      const response = await fetch(`${API_V1}/spaces/${selectedSpace.id}/access`, {
        method: 'DELETE',
        headers: {
          ...getAuthHeaders(),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ agent_id: agentId }),
      });

      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || 'Failed to revoke access');
      }

      setMembers(members.filter(m => m.agent_id !== agentId));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke access');
    }
  };

  const filteredSpaces = spaces.filter(space =>
    space.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
    space.description.toLowerCase().includes(searchQuery.toLowerCase())
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="w-8 h-8 animate-spin text-[#8B5CF6]" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Projects</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Organize memories into projects and share with your team
          </p>
        </div>
        <button
          onClick={() => setShowCreateSpace(true)}
          className="inline-flex items-center gap-2 px-4 py-2 bg-[#8B5CF6] hover:bg-[#7C3AED] text-white rounded-lg font-medium transition-colors"
        >
          <Plus className="w-4 h-4" />
          New Project
        </button>
      </div>

      {error && (
        <div className="p-4 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
        <input
          type="text"
          placeholder="Search projects..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="w-full pl-10 pr-4 py-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#8B5CF6]"
        />
      </div>

      {/* Projects Grid */}
      {filteredSpaces.length === 0 ? (
        <div className="text-center py-12 bg-gray-50 dark:bg-gray-800/50 rounded-xl border border-dashed border-gray-300 dark:border-gray-700">
          <FolderOpen className="w-12 h-12 mx-auto text-gray-400 mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-white mb-2">
            No projects yet
          </h3>
          <p className="text-gray-500 dark:text-gray-400 mb-4">
            Create a project to organize your memories
          </p>
          <button
            onClick={() => setShowCreateSpace(true)}
            className="inline-flex items-center gap-2 px-4 py-2 bg-[#8B5CF6] hover:bg-[#7C3AED] text-white rounded-lg font-medium transition-colors"
          >
            <FolderPlus className="w-4 h-4" />
            Create First Project
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredSpaces.map((space) => (
            <div
              key={space.id}
              onClick={() => setSelectedSpace(space)}
              className={clsx(
                'p-4 rounded-xl border cursor-pointer transition-all',
                selectedSpace?.id === space.id
                  ? 'border-[#8B5CF6] bg-[#8B5CF6]/5 ring-2 ring-[#8B5CF6]/20'
                  : 'border-gray-200 dark:border-gray-700 hover:border-[#8B5CF6]/50 bg-white dark:bg-gray-800'
              )}
            >
              <div className="flex items-start justify-between mb-3">
                <div className="flex items-center gap-2">
                  <div className="p-2 rounded-lg bg-[#8B5CF6]/10">
                    <FolderOpen className="w-5 h-5 text-[#8B5CF6]" />
                  </div>
                  <div>
                    <h3 className="font-semibold text-gray-900 dark:text-white">
                      {space.name}
                    </h3>
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      {space.members} member{space.members !== 1 ? 's' : ''}
                    </p>
                  </div>
                </div>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    deleteSpace(space.id);
                  }}
                  className="p-1 rounded hover:bg-red-100 dark:hover:bg-red-900/30 text-gray-400 hover:text-red-500"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
              
              {space.description && (
                <p className="text-sm text-gray-600 dark:text-gray-400 mb-3 line-clamp-2">
                  {space.description}
                </p>
              )}

              <div className="flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400">
                <span>
                  Created {new Date(space.created_at).toLocaleDateString()}
                </span>
                {space.memory_count !== undefined && (
                  <span>{space.memory_count} memories</span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Selected Project Details */}
      {selectedSpace && (
        <div className="mt-8 p-6 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
          <div className="flex items-center justify-between mb-6">
            <div>
              <h2 className="text-xl font-bold text-gray-900 dark:text-white">
                {selectedSpace.name}
              </h2>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Project ID: {selectedSpace.id}
              </p>
            </div>
            <button
              onClick={() => setShowGrantAccess(true)}
              className="inline-flex items-center gap-2 px-3 py-1.5 text-sm bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-300 rounded-lg transition-colors"
            >
              <Users className="w-4 h-4" />
              Share Access
            </button>
          </div>

          {/* Members List */}
          <div>
            <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">
              Access List
            </h3>
            {members.length === 0 ? (
              <p className="text-sm text-gray-500 dark:text-gray-400">
                No shared access yet. You're the only one with access.
              </p>
            ) : (
              <div className="space-y-2">
                {members.map((member) => (
                  <div
                    key={member.agent_id}
                    className="flex items-center justify-between p-3 rounded-lg bg-gray-50 dark:bg-gray-900"
                  >
                    <div className="flex items-center gap-3">
                      <div className="w-8 h-8 rounded-full bg-[#8B5CF6]/20 flex items-center justify-center">
                        <Users className="w-4 h-4 text-[#8B5CF6]" />
                      </div>
                      <div>
                        <p className="text-sm font-medium text-gray-900 dark:text-white">
                          {member.agent_id}
                        </p>
                        <p className="text-xs text-gray-500 dark:text-gray-400">
                          {member.permission} access
                        </p>
                      </div>
                    </div>
                    <button
                      onClick={() => revokeAccess(member.agent_id)}
                      className="text-sm text-red-500 hover:text-red-600"
                    >
                      Revoke
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Create Project Modal */}
      {showCreateSpace && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-xl p-6 w-full max-w-md mx-4">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-bold text-gray-900 dark:text-white">
                New Project
              </h2>
              <button
                onClick={() => setShowCreateSpace(false)}
                className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700"
              >
                <X className="w-5 h-5 text-gray-500" />
              </button>
            </div>

            <form onSubmit={createSpace} className="space-y-4">
              {formError && (
                <div className="p-3 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 text-sm">
                  {formError}
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Project Name
                </label>
                <input
                  type="text"
                  value={newSpaceName}
                  onChange={(e) => setNewSpaceName(e.target.value)}
                  placeholder="e.g., Product Research"
                  required
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#8B5CF6]"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Description (optional)
                </label>
                <textarea
                  value={newSpaceDescription}
                  onChange={(e) => setNewSpaceDescription(e.target.value)}
                  placeholder="What is this project for?"
                  rows={3}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#8B5CF6]"
                />
              </div>

              <div className="flex gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowCreateSpace(false)}
                  className="flex-1 px-4 py-2 rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={formLoading || !newSpaceName.trim()}
                  className="flex-1 px-4 py-2 rounded-lg bg-[#8B5CF6] hover:bg-[#7C3AED] text-white font-medium disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                >
                  {formLoading ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <>
                      <FolderPlus className="w-4 h-4" />
                      Create Project
                    </>
                  )}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Grant Access Modal */}
      {showGrantAccess && selectedSpace && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-xl p-6 w-full max-w-md mx-4">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-bold text-gray-900 dark:text-white">
                Share Access
              </h2>
              <button
                onClick={() => setShowGrantAccess(false)}
                className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700"
              >
                <X className="w-5 h-5 text-gray-500" />
              </button>
            </div>

            <form onSubmit={grantAccess} className="space-y-4">
              {formError && (
                <div className="p-3 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 text-sm">
                  {formError}
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Agent/User ID
                </label>
                <input
                  type="text"
                  value={grantAgentId}
                  onChange={(e) => setGrantAgentId(e.target.value)}
                  placeholder="user_abc123 or agent_xyz"
                  required
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#8B5CF6]"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Permission Level
                </label>
                <select
                  value={grantPermission}
                  onChange={(e) => setGrantPermission(e.target.value as 'read' | 'write' | 'admin')}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-[#8B5CF6]"
                >
                  <option value="read">Read — Can recall memories</option>
                  <option value="write">Write — Can store and recall</option>
                  <option value="admin">Admin — Full control</option>
                </select>
              </div>

              <div className="flex gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowGrantAccess(false)}
                  className="flex-1 px-4 py-2 rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={formLoading || !grantAgentId.trim()}
                  className="flex-1 px-4 py-2 rounded-lg bg-[#8B5CF6] hover:bg-[#7C3AED] text-white font-medium disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                >
                  {formLoading ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <>
                      <Users className="w-4 h-4" />
                      Grant Access
                    </>
                  )}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
