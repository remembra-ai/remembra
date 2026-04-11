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

interface Team {
  id: string;
  name: string;
}

interface TeamMember {
  user_id: string;
  email: string | null;
  name: string | null;
  role: string;
}

function slugifyProjectId(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64) || 'default';
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
  const [newSpaceProjectId, setNewSpaceProjectId] = useState('default');
  const [projectIdTouched, setProjectIdTouched] = useState(false);
  const [grantAgentId, setGrantAgentId] = useState('');
  const [grantPermission, setGrantPermission] = useState<'read' | 'write' | 'admin'>('read');
  const [formLoading, setFormLoading] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  
  // Team members for share access picker
  const [teamMembers, setTeamMembers] = useState<TeamMember[]>([]);
  const [teamMembersLoading, setTeamMembersLoading] = useState(false);
  const [memberSearch, setMemberSearch] = useState('');
  const [showMemberDropdown, setShowMemberDropdown] = useState(false);

  const closeCreateSpaceModal = () => {
    setShowCreateSpace(false);
    setFormError(null);
    setNewSpaceName('');
    setNewSpaceDescription('');
    setNewSpaceProjectId('default');
    setProjectIdTouched(false);
  };

  const getAuthHeaders = () => {
    const token = localStorage.getItem('remembra_jwt_token');
    if (token) {
      return { 'Authorization': `Bearer ${token}` };
    }
    const apiKey = localStorage.getItem('remembra_api_key');
    if (apiKey) {
      return { 'X-API-Key': apiKey };
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

  // Fetch team members when grant access modal opens
  useEffect(() => {
    if (showGrantAccess) {
      fetchTeamMembers();
    }
  }, [showGrantAccess]);

  const fetchSpaces = async () => {
    try {
      setLoading(true);
      setError(null);
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
      const rawSpaces: Space[] = Array.isArray(data) ? data : data.spaces || [];
      const detailedSpaces = await Promise.all(rawSpaces.map(async (space) => {
        try {
          const detailResponse = await fetch(`${API_V1}/spaces/${space.id}`, {
            headers: getAuthHeaders(),
          });

          if (!detailResponse.ok) {
            return { ...space, members: space.members ?? 1 };
          }

          const detail = await detailResponse.json();
          return {
            ...space,
            members: detail.members ?? space.members ?? 1,
            memory_count: detail.memory_count ?? space.memory_count,
          };
        } catch {
          return { ...space, members: space.members ?? 1 };
        }
      }));

      setSpaces(detailedSpaces);
      
      // Auto-select first space if available
      if (detailedSpaces.length > 0 && !selectedSpace) {
        setSelectedSpace(detailedSpaces[0]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load projects');
    } finally {
      setLoading(false);
    }
  };

  const fetchMembers = async (spaceId: string) => {
    try {
      const response = await fetch(`${API_V1}/spaces/${spaceId}/members`, {
        headers: getAuthHeaders(),
      });
      
      if (!response.ok) {
        return;
      }
      
      const data = await response.json();
      setMembers(Array.isArray(data) ? data : data.members || []);
    } catch (err) {
      console.error('Failed to fetch members:', err);
    }
  };

  const fetchTeamMembers = async () => {
    try {
      setTeamMembersLoading(true);
      // First get all teams
      const teamsResponse = await fetch(`${API_V1}/teams`, {
        headers: getAuthHeaders(),
      });
      
      if (!teamsResponse.ok) {
        return;
      }
      
      const teams: Team[] = await teamsResponse.json();
      
      // Fetch members from all teams
      const allMembers: TeamMember[] = [];
      const seenUserIds = new Set<string>();
      
      for (const team of teams) {
        try {
          const membersResponse = await fetch(`${API_V1}/teams/${team.id}/members`, {
            headers: getAuthHeaders(),
          });
          
          if (membersResponse.ok) {
            const members: TeamMember[] = await membersResponse.json();
            for (const member of members) {
              if (!seenUserIds.has(member.user_id)) {
                seenUserIds.add(member.user_id);
                allMembers.push(member);
              }
            }
          }
        } catch {
          // Skip this team if members fetch fails
        }
      }
      
      setTeamMembers(allMembers);
    } catch (err) {
      console.error('Failed to fetch team members:', err);
    } finally {
      setTeamMembersLoading(false);
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
          project_id: newSpaceProjectId.trim(),
        }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to create project');
      }

      const newSpace = await response.json();
      const normalizedSpace = { ...newSpace, members: newSpace.members ?? 1 };
      setSpaces((prev) => [...prev, normalizedSpace]);
      setSelectedSpace(normalizedSpace);
      closeCreateSpaceModal();
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

      const remainingSpaces = spaces.filter(s => s.id !== spaceId);
      setSpaces(remainingSpaces);
      if (selectedSpace?.id === spaceId) {
        setSelectedSpace(remainingSpaces[0] || null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete project');
    }
  };

  const wipeProjectMemories = async (space: Space) => {
    const memoryCount = space.memory_count || 0;
    if (!confirm(`⚠️ DANGER: This will permanently delete ALL ${memoryCount} memories in "${space.name}".\n\nThis action cannot be undone. Are you absolutely sure?`)) {
      return;
    }
    
    // Double confirmation for safety
    const confirmText = prompt(`Type "${space.project_id}" to confirm deletion:`);
    if (confirmText !== space.project_id) {
      alert('Confirmation text did not match. Deletion cancelled.');
      return;
    }

    try {
      const response = await fetch(`${API_V1}/memories?project_id=${space.project_id}`, {
        method: 'DELETE',
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Failed to wipe project memories');
      }

      const result = await response.json();
      alert(`Successfully deleted ${result.deleted_memories || 0} memories from "${space.name}".`);
      
      // Refresh the spaces list to update memory counts
      fetchSpaces();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to wipe project memories');
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
      setMemberSearch('');
      setShowMemberDropdown(false);
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
                Space ID: {selectedSpace.id} · Namespace: {selectedSpace.project_id}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setShowGrantAccess(true)}
                className="inline-flex items-center gap-2 px-3 py-1.5 text-sm bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-300 rounded-lg transition-colors"
              >
                <Users className="w-4 h-4" />
                Share Access
              </button>
              <button
                onClick={() => wipeProjectMemories(selectedSpace)}
                className="inline-flex items-center gap-2 px-3 py-1.5 text-sm bg-red-100 dark:bg-red-900/30 hover:bg-red-200 dark:hover:bg-red-900/50 text-red-700 dark:text-red-300 rounded-lg transition-colors"
              >
                <Trash2 className="w-4 h-4" />
                Wipe All Memories
              </button>
            </div>
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
                onClick={closeCreateSpaceModal}
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
                  onChange={(e) => {
                    const value = e.target.value;
                    setNewSpaceName(value);
                    if (!projectIdTouched) {
                      setNewSpaceProjectId(slugifyProjectId(value));
                    }
                  }}
                  placeholder="e.g., Product Research"
                  required
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#8B5CF6]"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Project Namespace
                </label>
                <input
                  type="text"
                  value={newSpaceProjectId}
                  onChange={(e) => {
                    setProjectIdTouched(true);
                    setNewSpaceProjectId(slugifyProjectId(e.target.value));
                  }}
                  placeholder="e.g., product-research"
                  required
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#8B5CF6]"
                />
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  This becomes the isolated `project_id` used by memories, recall, analytics, and API clients.
                </p>
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
                  onClick={closeCreateSpaceModal}
                  className="flex-1 px-4 py-2 rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={formLoading || !newSpaceName.trim() || !newSpaceProjectId.trim()}
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
                onClick={() => {
                  setShowGrantAccess(false);
                  setMemberSearch('');
                  setShowMemberDropdown(false);
                }}
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

              <div className="relative">
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Team Member or User ID
                </label>
                
                {/* Search/Select Input */}
                <div className="relative">
                  <input
                    type="text"
                    value={grantAgentId || memberSearch}
                    onChange={(e) => {
                      const value = e.target.value;
                      setMemberSearch(value);
                      setGrantAgentId(value);
                      setShowMemberDropdown(true);
                    }}
                    onFocus={() => setShowMemberDropdown(true)}
                    placeholder={teamMembersLoading ? "Loading team members..." : "Search team members or enter user ID"}
                    required
                    className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#8B5CF6]"
                  />
                  {teamMembersLoading && (
                    <div className="absolute right-3 top-1/2 -translate-y-1/2">
                      <Loader2 className="w-4 h-4 animate-spin text-gray-400" />
                    </div>
                  )}
                </div>
                
                {/* Dropdown with team members */}
                {showMemberDropdown && teamMembers.length > 0 && (
                  <div className="absolute z-10 w-full mt-1 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg max-h-48 overflow-y-auto">
                    {teamMembers
                      .filter(member => {
                        const search = memberSearch.toLowerCase();
                        return (
                          !search ||
                          member.user_id.toLowerCase().includes(search) ||
                          (member.email && member.email.toLowerCase().includes(search)) ||
                          (member.name && member.name.toLowerCase().includes(search))
                        );
                      })
                      .map((member) => (
                        <button
                          key={member.user_id}
                          type="button"
                          onClick={() => {
                            setGrantAgentId(member.user_id);
                            setMemberSearch('');
                            setShowMemberDropdown(false);
                          }}
                          className="w-full px-3 py-2 text-left hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-3 transition-colors"
                        >
                          <div className="w-8 h-8 rounded-full bg-[#8B5CF6]/20 flex items-center justify-center flex-shrink-0">
                            <Users className="w-4 h-4 text-[#8B5CF6]" />
                          </div>
                          <div className="min-w-0 flex-1">
                            <p className="text-sm font-medium text-gray-900 dark:text-white truncate">
                              {member.name || member.email || member.user_id}
                            </p>
                            {(member.name || member.email) && (
                              <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                                {member.email || member.user_id}
                              </p>
                            )}
                          </div>
                          <span className="text-xs text-gray-400 capitalize flex-shrink-0">
                            {member.role}
                          </span>
                        </button>
                      ))}
                    {teamMembers.filter(member => {
                      const search = memberSearch.toLowerCase();
                      return (
                        !search ||
                        member.user_id.toLowerCase().includes(search) ||
                        (member.email && member.email.toLowerCase().includes(search)) ||
                        (member.name && member.name.toLowerCase().includes(search))
                      );
                    }).length === 0 && (
                      <div className="px-3 py-2 text-sm text-gray-500 dark:text-gray-400">
                        No matching team members. Enter a user ID manually.
                      </div>
                    )}
                  </div>
                )}
                
                {/* Click outside to close dropdown */}
                {showMemberDropdown && (
                  <div 
                    className="fixed inset-0 z-0" 
                    onClick={() => setShowMemberDropdown(false)}
                  />
                )}
                
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  Select a team member or enter any user/agent ID
                </p>
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
                  onClick={() => {
                    setShowGrantAccess(false);
                    setMemberSearch('');
                    setShowMemberDropdown(false);
                  }}
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
