import { useState, useEffect } from 'react';
import { Users, Plus, Mail, UserPlus, Trash2, Crown, Shield, User, Eye, X, Copy, Check, FolderOpen, Link2, LogOut } from 'lucide-react';
import clsx from 'clsx';
import { API_V1 } from '../config';

interface Team {
  id: string;
  name: string;
  slug: string;
  description: string;
  owner_id: string;
  plan: string;
  max_seats: number;
  used_seats: number;
  created_at: string;
  role: string;
}

interface TeamMember {
  user_id: string;
  email: string | null;
  name: string | null;
  role: string;
  invited_by: string | null;
  joined_at: string;
}

interface PendingInvite {
  id: string;
  email: string;
  role: string;
  invited_by: string;
  status: string;
  expires_at: string;
  created_at: string;
}

interface InviteResponse {
  id: string;
  team_id: string;
  team_name: string;
  email: string;
  role: string;
  invite_url: string;
  expires_at: string;
  created_at: string;
}

interface LinkedProject {
  id: string;
  name: string;
  description: string;
  project_id: string;
}

const roleIcons: Record<string, React.ElementType> = {
  owner: Crown,
  admin: Shield,
  member: User,
  viewer: Eye,
};

const roleColors: Record<string, string> = {
  owner: 'text-yellow-500',
  admin: 'text-purple-500',
  member: 'text-blue-500',
  viewer: 'text-gray-500',
};

export function Teams() {
  const [teams, setTeams] = useState<Team[]>([]);
  const [selectedTeam, setSelectedTeam] = useState<Team | null>(null);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [pendingInvites, setPendingInvites] = useState<PendingInvite[]>([]);
  const [linkedProjects, setLinkedProjects] = useState<LinkedProject[]>([]);
  const [availableProjects, setAvailableProjects] = useState<LinkedProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [teamActionError, setTeamActionError] = useState<string | null>(null);
  
  // Modals
  const [showCreateTeam, setShowCreateTeam] = useState(false);
  const [showInviteMember, setShowInviteMember] = useState(false);
  const [lastInvite, setLastInvite] = useState<InviteResponse | null>(null);
  const [copiedUrl, setCopiedUrl] = useState(false);
  
  // Form states
  const [newTeamName, setNewTeamName] = useState('');
  const [newTeamDescription, setNewTeamDescription] = useState('');
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState('member');
  const [formLoading, setFormLoading] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [roleUpdateLoading, setRoleUpdateLoading] = useState<string | null>(null);
  const [spaceActionLoading, setSpaceActionLoading] = useState(false);
  const [selectedSpaceId, setSelectedSpaceId] = useState('');

  const getAuthHeaders = (): Record<string, string> => {
    const token = localStorage.getItem('remembra_jwt_token');
    if (token) {
      return { 'Authorization': `Bearer ${token}` };
    }
    // Fallback to API key if no JWT
    const apiKey = localStorage.getItem('remembra_api_key');
    if (apiKey) {
      return { 'X-API-Key': apiKey };
    }
    return {};
  };

  // Fetch teams
  useEffect(() => {
    fetchTeams();
  }, []);

  // Fetch members when team is selected
  useEffect(() => {
    if (selectedTeam) {
      fetchMembers(selectedTeam.id);
      fetchLinkedProjects(selectedTeam.id);
      if (selectedTeam.role === 'owner' || selectedTeam.role === 'admin') {
        fetchInvites(selectedTeam.id);
        fetchAvailableProjects();
      }
    }
  }, [selectedTeam]);

  const fetchTeams = async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_V1}/teams`, {
        headers: getAuthHeaders(),
      });
      
      if (!response.ok) {
        throw new Error('Failed to fetch teams');
      }
      
      const data = await response.json();
      setTeams(data);
      
      // Auto-select first team if available
      if (data.length > 0 && !selectedTeam) {
        setSelectedTeam(data[0]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load teams');
    } finally {
      setLoading(false);
    }
  };

  const fetchMembers = async (teamId: string) => {
    try {
      const response = await fetch(`${API_V1}/teams/${teamId}/members`, {
        headers: getAuthHeaders(),
      });
      
      if (!response.ok) {
        throw new Error('Failed to fetch members');
      }
      
      const data = await response.json();
      setMembers(data);
    } catch (err) {
      console.error('Failed to fetch members:', err);
    }
  };

  const fetchInvites = async (teamId: string) => {
    try {
      const response = await fetch(`${API_V1}/teams/${teamId}/invites`, {
        headers: getAuthHeaders(),
      });
      
      if (!response.ok) {
        return; // May not have permission
      }
      
      const data = await response.json();
      setPendingInvites(data);
    } catch (err) {
      console.error('Failed to fetch invites:', err);
    }
  };

  const fetchAvailableProjects = async () => {
    try {
      const response = await fetch(`${API_V1}/spaces`, {
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        return;
      }

      const data = await response.json();
      const spaces = Array.isArray(data) ? data : data.spaces || [];
      setAvailableProjects(spaces);
    } catch (err) {
      console.error('Failed to fetch projects:', err);
    }
  };

  const fetchLinkedProjects = async (teamId: string) => {
    try {
      const response = await fetch(`${API_V1}/teams/${teamId}/spaces`, {
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        setLinkedProjects([]);
        return;
      }

      const spaceIds: string[] = await response.json();
      const resolved = await Promise.all(spaceIds.map(async (spaceId) => {
        try {
          const detailResponse = await fetch(`${API_V1}/spaces/${spaceId}`, {
            headers: getAuthHeaders(),
          });

          if (!detailResponse.ok) {
            return null;
          }

          const detail = await detailResponse.json();
          return {
            id: detail.id,
            name: detail.name,
            description: detail.description,
            project_id: detail.project_id,
          } satisfies LinkedProject;
        } catch {
          return null;
        }
      }));

      setLinkedProjects(resolved.filter((item): item is LinkedProject => item !== null));
    } catch (err) {
      console.error('Failed to fetch linked projects:', err);
      setLinkedProjects([]);
    }
  };

  const createTeam = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormLoading(true);
    setFormError(null);

    try {
      const response = await fetch(`${API_V1}/teams`, {
        method: 'POST',
        headers: {
          ...getAuthHeaders(),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          name: newTeamName,
          description: newTeamDescription,
        }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to create team');
      }

      const newTeam = await response.json();
      setTeams([...teams, newTeam]);
      setSelectedTeam(newTeam);
      setShowCreateTeam(false);
      setNewTeamName('');
      setNewTeamDescription('');
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to create team');
    } finally {
      setFormLoading(false);
    }
  };

  const inviteMember = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedTeam) return;
    
    setFormLoading(true);
    setFormError(null);

    try {
      const response = await fetch(`${API_V1}/teams/${selectedTeam.id}/invites`, {
        method: 'POST',
        headers: {
          ...getAuthHeaders(),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          email: inviteEmail,
          role: inviteRole,
        }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to send invite');
      }

      const invite = await response.json();
      setLastInvite(invite);
      setInviteEmail('');
      setInviteRole('member');
      fetchInvites(selectedTeam.id);
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to send invite');
    } finally {
      setFormLoading(false);
    }
  };

  const revokeInvite = async (inviteId: string) => {
    if (!selectedTeam) return;
    
    try {
      const response = await fetch(`${API_V1}/teams/${selectedTeam.id}/invites/${inviteId}`, {
        method: 'DELETE',
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        throw new Error('Failed to revoke invite');
      }

      fetchInvites(selectedTeam.id);
    } catch (err) {
      console.error('Failed to revoke invite:', err);
    }
  };

  const removeMember = async (userId: string) => {
    if (!selectedTeam) return;
    
    if (!confirm('Are you sure you want to remove this member?')) return;
    
    try {
      const response = await fetch(`${API_V1}/teams/${selectedTeam.id}/members/${userId}`, {
        method: 'DELETE',
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to remove member');
      }

      fetchMembers(selectedTeam.id);
      // Refresh team to update seat count
      fetchTeams();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to remove member');
    }
  };

  const updateMemberRole = async (memberId: string, role: string) => {
    if (!selectedTeam) return;

    setRoleUpdateLoading(memberId);
    setTeamActionError(null);

    try {
      const response = await fetch(`${API_V1}/teams/${selectedTeam.id}/members/${memberId}/role`, {
        method: 'PATCH',
        headers: {
          ...getAuthHeaders(),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ role }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to update member role');
      }

      await fetchMembers(selectedTeam.id);
      await fetchTeams();
    } catch (err) {
      setTeamActionError(err instanceof Error ? err.message : 'Failed to update member role');
    } finally {
      setRoleUpdateLoading(null);
    }
  };

  const linkProject = async () => {
    if (!selectedTeam || !selectedSpaceId) return;

    setSpaceActionLoading(true);
    setTeamActionError(null);

    try {
      const response = await fetch(`${API_V1}/teams/${selectedTeam.id}/spaces`, {
        method: 'POST',
        headers: {
          ...getAuthHeaders(),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ space_id: selectedSpaceId }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to link project');
      }

      setSelectedSpaceId('');
      await fetchLinkedProjects(selectedTeam.id);
    } catch (err) {
      setTeamActionError(err instanceof Error ? err.message : 'Failed to link project');
    } finally {
      setSpaceActionLoading(false);
    }
  };

  const unlinkProject = async (spaceId: string) => {
    if (!selectedTeam) return;

    setSpaceActionLoading(true);
    setTeamActionError(null);

    try {
      const response = await fetch(`${API_V1}/teams/${selectedTeam.id}/spaces/${spaceId}`, {
        method: 'DELETE',
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to unlink project');
      }

      await fetchLinkedProjects(selectedTeam.id);
    } catch (err) {
      setTeamActionError(err instanceof Error ? err.message : 'Failed to unlink project');
    } finally {
      setSpaceActionLoading(false);
    }
  };

  const leaveTeam = async () => {
    if (!selectedTeam) return;
    if (!confirm(`Leave ${selectedTeam.name}?`)) return;

    setTeamActionError(null);

    try {
      const response = await fetch(`${API_V1}/teams/${selectedTeam.id}/leave`, {
        method: 'POST',
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to leave team');
      }

      const remainingTeams = teams.filter((team) => team.id !== selectedTeam.id);
      setTeams(remainingTeams);
      setSelectedTeam(remainingTeams[0] || null);
      setMembers([]);
      setPendingInvites([]);
      setLinkedProjects([]);
    } catch (err) {
      setTeamActionError(err instanceof Error ? err.message : 'Failed to leave team');
    }
  };

  const copyInviteUrl = async () => {
    if (!lastInvite) return;
    
    try {
      await navigator.clipboard.writeText(lastInvite.invite_url);
      setCopiedUrl(true);
      setTimeout(() => setCopiedUrl(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  const canManageTeam = selectedTeam?.role === 'owner' || selectedTeam?.role === 'admin';
  const selectableProjects = availableProjects.filter(
    (project) => !linkedProjects.some((linked) => linked.id === project.id),
  );

  if (loading && teams.length === 0) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-signal"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-[hsl(var(--foreground))]">Teams</h1>
          <p className="text-[hsl(var(--muted-foreground))]">
            Collaborate with your team using shared memory spaces
          </p>
        </div>
        
        <button
          onClick={() => setShowCreateTeam(true)}
          className={clsx(
            'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg',
            'bg-accent hover:bg-accent-hover text-white',
            'font-medium text-sm transition-colors',
            'shadow-lg shadow-purple-500/20'
          )}
        >
          <Plus className="w-4 h-4" />
          Create Team
        </button>
      </div>

      {error && (
        <div className="p-4 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400">
          {error}
        </div>
      )}

      {/* Teams Grid */}
      {teams.length === 0 ? (
        <div className="text-center py-12 bg-[hsl(var(--card))] rounded-xl border border-[hsl(var(--border))]">
          <Users className="w-12 h-12 mx-auto text-[hsl(var(--muted-foreground))] mb-4" />
          <h3 className="text-lg font-semibold text-[hsl(var(--foreground))] mb-2">
            No teams yet
          </h3>
          <p className="text-[hsl(var(--muted-foreground))] mb-4">
            Create a team to start collaborating with others
          </p>
          <button
            onClick={() => setShowCreateTeam(true)}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-accent hover:bg-accent-hover text-white font-medium text-sm"
          >
            <Plus className="w-4 h-4" />
            Create Your First Team
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Team List */}
          <div className="space-y-3">
            <h3 className="text-sm font-semibold text-[hsl(var(--muted-foreground))] uppercase tracking-wider">
              Your Teams
            </h3>
            {teams.map((team) => (
              <button
                key={team.id}
                onClick={() => setSelectedTeam(team)}
                className={clsx(
                  'w-full p-4 rounded-xl text-left transition-all',
                  'border',
                  selectedTeam?.id === team.id
                    ? 'bg-signal/10 border-signal/50'
                    : 'bg-[hsl(var(--card))] border-[hsl(var(--border))] hover:border-signal/30'
                )}
              >
                <div className="flex items-center justify-between mb-2">
                  <h4 className="font-semibold text-[hsl(var(--foreground))]">{team.name}</h4>
                  <span className={clsx('text-xs font-medium capitalize', roleColors[team.role])}>
                    {team.role}
                  </span>
                </div>
                <p className="text-sm text-[hsl(var(--muted-foreground))] mb-2 line-clamp-1">
                  {team.description || 'No description'}
                </p>
                <div className="flex items-center gap-4 text-xs text-[hsl(var(--muted-foreground))]">
                  <span className="flex items-center gap-1">
                    <Users className="w-3 h-3" />
                    {team.used_seats}/{team.max_seats} seats
                  </span>
                  <span className="capitalize">{team.plan} plan</span>
                </div>
              </button>
            ))}
          </div>

          {/* Team Details */}
          {selectedTeam && (
            <div className="lg:col-span-2 space-y-6">
              {/* Team Header */}
              <div className="p-6 rounded-xl bg-[hsl(var(--card))] border border-[hsl(var(--border))]">
                <div className="flex items-start justify-between mb-4">
                  <div>
                    <h2 className="text-xl font-bold text-[hsl(var(--foreground))]">
                      {selectedTeam.name}
                    </h2>
                    <p className="text-[hsl(var(--muted-foreground))]">
                      {selectedTeam.description || 'No description'}
                    </p>
                  </div>
                  
                  {canManageTeam && (
                    <button
                      onClick={() => setShowInviteMember(true)}
                      className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-accent hover:bg-accent-hover text-white font-medium text-sm"
                    >
                      <UserPlus className="w-4 h-4" />
                      Invite
                    </button>
                  )}
                </div>

                {teamActionError && (
                  <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
                    {teamActionError}
                  </div>
                )}
                
                <div className="grid grid-cols-3 gap-4">
                  <div className="p-3 rounded-lg bg-[hsl(var(--muted))]">
                    <p className="text-xs text-[hsl(var(--muted-foreground))]">Seats Used</p>
                    <p className="text-lg font-semibold text-[hsl(var(--foreground))]">
                      {selectedTeam.used_seats} / {selectedTeam.max_seats}
                    </p>
                  </div>
                  <div className="p-3 rounded-lg bg-[hsl(var(--muted))]">
                    <p className="text-xs text-[hsl(var(--muted-foreground))]">Plan</p>
                    <p className="text-lg font-semibold text-[hsl(var(--foreground))] capitalize">
                      {selectedTeam.plan}
                    </p>
                  </div>
                  <div className="p-3 rounded-lg bg-[hsl(var(--muted))]">
                    <p className="text-xs text-[hsl(var(--muted-foreground))]">Your Role</p>
                    <p className="text-lg font-semibold text-[hsl(var(--foreground))] capitalize">
                      {selectedTeam.role}
                    </p>
                  </div>
                </div>

                {selectedTeam.role !== 'owner' && (
                  <div className="mt-4 flex justify-end">
                    <button
                      onClick={leaveTeam}
                      className="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-[hsl(var(--border))] text-[hsl(var(--muted-foreground))] hover:text-red-500 hover:border-red-500/30 hover:bg-red-500/5 transition-colors text-sm"
                    >
                      <LogOut className="w-4 h-4" />
                      Leave Team
                    </button>
                  </div>
                )}
              </div>

              {/* Members */}
              <div className="p-6 rounded-xl bg-[hsl(var(--card))] border border-[hsl(var(--border))]">
                <h3 className="text-lg font-semibold text-[hsl(var(--foreground))] mb-4">
                  Members ({members.length})
                </h3>
                
                <div className="space-y-3">
                  {members.map((member) => {
                    const RoleIcon = roleIcons[member.role] || User;
                    return (
                      <div
                        key={member.user_id}
                        className="flex items-center justify-between p-3 rounded-lg bg-[hsl(var(--muted))]"
                      >
                        <div className="flex items-center gap-3">
                          <div className="w-10 h-10 rounded-full bg-signal/20 flex items-center justify-center">
                            <RoleIcon className={clsx('w-5 h-5', roleColors[member.role])} />
                          </div>
                          <div>
                            <p className="font-medium text-[hsl(var(--foreground))]">
                              {member.name || member.email || 'Unknown'}
                            </p>
                            {member.email && member.name && (
                              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                                {member.email}
                              </p>
                            )}
                          </div>
                        </div>
                        
                        <div className="flex items-center gap-3">
                          <span className={clsx('text-sm font-medium capitalize', roleColors[member.role])}>
                            {member.role}
                          </span>

                          {canManageTeam && member.role !== 'owner' && (
                            <select
                              value={member.role}
                              onChange={(event) => updateMemberRole(member.user_id, event.target.value)}
                              disabled={roleUpdateLoading === member.user_id}
                              className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-1 text-xs text-[hsl(var(--foreground))]"
                            >
                              <option value="viewer">Viewer</option>
                              <option value="member">Member</option>
                              <option value="admin">Admin</option>
                            </select>
                          )}
                          
                          {canManageTeam && member.role !== 'owner' && (
                            <button
                              onClick={() => removeMember(member.user_id)}
                              className="p-2 rounded-lg hover:bg-red-500/10 text-[hsl(var(--muted-foreground))] hover:text-red-500 transition-colors"
                              title="Remove member"
                            >
                              <Trash2 className="w-4 h-4" />
                            </button>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Linked Projects */}
              <div className="p-6 rounded-xl bg-[hsl(var(--card))] border border-[hsl(var(--border))]">
                <div className="flex items-center gap-2 mb-4">
                  <FolderOpen className="w-5 h-5 text-signal-ink" />
                  <h3 className="text-lg font-semibold text-[hsl(var(--foreground))]">
                    Linked Projects ({linkedProjects.length})
                  </h3>
                </div>

                {canManageTeam && (
                  <div className="mb-4 flex flex-col sm:flex-row gap-3">
                    <select
                      value={selectedSpaceId}
                      onChange={(event) => setSelectedSpaceId(event.target.value)}
                      className="flex-1 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-3 py-2 text-sm text-[hsl(var(--foreground))]"
                    >
                      <option value="">Select a project to link</option>
                      {selectableProjects.map((project) => (
                        <option key={project.id} value={project.id}>
                          {project.name} ({project.project_id})
                        </option>
                      ))}
                    </select>
                    <button
                      onClick={linkProject}
                      disabled={spaceActionLoading || !selectedSpaceId}
                      className="inline-flex items-center justify-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <Link2 className="w-4 h-4" />
                      Link Project
                    </button>
                  </div>
                )}

                {linkedProjects.length === 0 ? (
                  <p className="text-sm text-[hsl(var(--muted-foreground))]">
                    No projects linked yet.
                  </p>
                ) : (
                  <div className="space-y-3">
                    {linkedProjects.map((project) => (
                      <div
                        key={project.id}
                        className="flex items-center justify-between rounded-lg bg-[hsl(var(--muted))] px-4 py-3"
                      >
                        <div>
                          <div className="font-medium text-[hsl(var(--foreground))]">{project.name}</div>
                          <div className="text-xs text-[hsl(var(--muted-foreground))]">
                            Namespace: <code>{project.project_id}</code>
                          </div>
                        </div>

                        {canManageTeam && (
                          <button
                            onClick={() => unlinkProject(project.id)}
                            disabled={spaceActionLoading}
                            className="rounded-lg p-2 text-[hsl(var(--muted-foreground))] hover:bg-red-500/10 hover:text-red-500 transition-colors"
                            title="Unlink project"
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Pending Invites */}
              {canManageTeam && pendingInvites.length > 0 && (
                <div className="p-6 rounded-xl bg-[hsl(var(--card))] border border-[hsl(var(--border))]">
                  <h3 className="text-lg font-semibold text-[hsl(var(--foreground))] mb-4">
                    Pending Invites ({pendingInvites.length})
                  </h3>
                  
                  <div className="space-y-3">
                    {pendingInvites.map((invite) => (
                      <div
                        key={invite.id}
                        className="flex items-center justify-between p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/20"
                      >
                        <div className="flex items-center gap-3">
                          <Mail className="w-5 h-5 text-yellow-500" />
                          <div>
                            <p className="font-medium text-[hsl(var(--foreground))]">
                              {invite.email}
                            </p>
                            <p className="text-sm text-[hsl(var(--muted-foreground))]">
                              Invited as {invite.role} • Expires {new Date(invite.expires_at).toLocaleDateString()}
                            </p>
                          </div>
                        </div>
                        
                        <button
                          onClick={() => revokeInvite(invite.id)}
                          className="p-2 rounded-lg hover:bg-red-500/10 text-[hsl(var(--muted-foreground))] hover:text-red-500 transition-colors"
                          title="Revoke invite"
                        >
                          <X className="w-4 h-4" />
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Create Team Modal */}
      {showCreateTeam && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[hsl(var(--card))] rounded-xl p-6 w-full max-w-md mx-4 border border-[hsl(var(--border))]">
            <h2 className="text-xl font-bold text-[hsl(var(--foreground))] mb-4">
              Create Team
            </h2>
            
            <form onSubmit={createTeam} className="space-y-4">
              {formError && (
                <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
                  {formError}
                </div>
              )}
              
              <div>
                <label className="block text-sm font-medium text-[hsl(var(--foreground))] mb-1">
                  Team Name *
                </label>
                <input
                  type="text"
                  value={newTeamName}
                  onChange={(e) => setNewTeamName(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-[hsl(var(--muted))] border border-[hsl(var(--border))] text-[hsl(var(--foreground))] focus:outline-none focus:ring-2 focus:ring-signal"
                  placeholder="My Team"
                  required
                />
              </div>
              
              <div>
                <label className="block text-sm font-medium text-[hsl(var(--foreground))] mb-1">
                  Description
                </label>
                <textarea
                  value={newTeamDescription}
                  onChange={(e) => setNewTeamDescription(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-[hsl(var(--muted))] border border-[hsl(var(--border))] text-[hsl(var(--foreground))] focus:outline-none focus:ring-2 focus:ring-signal resize-none"
                  rows={3}
                  placeholder="What's this team for?"
                />
              </div>
              
              <div className="flex justify-end gap-3 pt-4">
                <button
                  type="button"
                  onClick={() => {
                    setShowCreateTeam(false);
                    setFormError(null);
                  }}
                  className="px-4 py-2 rounded-lg text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={formLoading || !newTeamName.trim()}
                  className={clsx(
                    'px-4 py-2 rounded-lg bg-accent hover:bg-accent-hover text-white font-medium',
                    'disabled:opacity-50 disabled:cursor-not-allowed'
                  )}
                >
                  {formLoading ? 'Creating...' : 'Create Team'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Invite Member Modal */}
      {showInviteMember && selectedTeam && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[hsl(var(--card))] rounded-xl p-6 w-full max-w-md mx-4 border border-[hsl(var(--border))]">
            <h2 className="text-xl font-bold text-[hsl(var(--foreground))] mb-4">
              Invite to {selectedTeam.name}
            </h2>
            
            {lastInvite ? (
              <div className="space-y-4">
                <div className="p-4 rounded-lg bg-green-500/10 border border-green-500/20">
                  <p className="text-green-400 font-medium mb-2">
                    ✓ Invite sent to {lastInvite.email}
                  </p>
                  <p className="text-sm text-[hsl(var(--muted-foreground))]">
                    They'll receive an email with a link to join. You can also share the link directly:
                  </p>
                </div>
                
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={lastInvite.invite_url}
                    readOnly
                    className="flex-1 px-3 py-2 rounded-lg bg-[hsl(var(--muted))] border border-[hsl(var(--border))] text-[hsl(var(--foreground))] text-sm"
                  />
                  <button
                    onClick={copyInviteUrl}
                    className="px-3 py-2 rounded-lg bg-[hsl(var(--muted))] hover:bg-[hsl(var(--muted))]/80 text-[hsl(var(--foreground))] transition-colors"
                  >
                    {copiedUrl ? <Check className="w-5 h-5 text-green-500" /> : <Copy className="w-5 h-5" />}
                  </button>
                </div>
                
                <div className="flex justify-end gap-3 pt-4">
                  <button
                    onClick={() => {
                      setLastInvite(null);
                    }}
                    className="px-4 py-2 rounded-lg text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
                  >
                    Invite Another
                  </button>
                  <button
                    onClick={() => {
                      setShowInviteMember(false);
                      setLastInvite(null);
                    }}
                    className="px-4 py-2 rounded-lg bg-accent hover:bg-accent-hover text-white font-medium"
                  >
                    Done
                  </button>
                </div>
              </div>
            ) : (
              <form onSubmit={inviteMember} className="space-y-4">
                {formError && (
                  <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
                    {formError}
                  </div>
                )}
                
                <div>
                  <label className="block text-sm font-medium text-[hsl(var(--foreground))] mb-1">
                    Email Address *
                  </label>
                  <input
                    type="email"
                    value={inviteEmail}
                    onChange={(e) => setInviteEmail(e.target.value)}
                    className="w-full px-3 py-2 rounded-lg bg-[hsl(var(--muted))] border border-[hsl(var(--border))] text-[hsl(var(--foreground))] focus:outline-none focus:ring-2 focus:ring-signal"
                    placeholder="colleague@company.com"
                    required
                  />
                </div>
                
                <div>
                  <label className="block text-sm font-medium text-[hsl(var(--foreground))] mb-1">
                    Role
                  </label>
                  <select
                    value={inviteRole}
                    onChange={(e) => setInviteRole(e.target.value)}
                    className="w-full px-3 py-2 rounded-lg bg-[hsl(var(--muted))] border border-[hsl(var(--border))] text-[hsl(var(--foreground))] focus:outline-none focus:ring-2 focus:ring-signal"
                  >
                    <option value="viewer">Viewer — Read-only access</option>
                    <option value="member">Member — Can create and edit</option>
                    <option value="admin">Admin — Can manage team</option>
                  </select>
                </div>
                
                {selectedTeam.used_seats >= selectedTeam.max_seats && (
                  <div className="p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/20 text-yellow-400 text-sm">
                    ⚠️ Team has reached maximum seats ({selectedTeam.max_seats}). 
                    Upgrade your plan to add more members.
                  </div>
                )}
                
                <div className="flex justify-end gap-3 pt-4">
                  <button
                    type="button"
                    onClick={() => {
                      setShowInviteMember(false);
                      setFormError(null);
                    }}
                    className="px-4 py-2 rounded-lg text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={formLoading || !inviteEmail.trim() || selectedTeam.used_seats >= selectedTeam.max_seats}
                    className={clsx(
                      'px-4 py-2 rounded-lg bg-accent hover:bg-accent-hover text-white font-medium',
                      'disabled:opacity-50 disabled:cursor-not-allowed'
                    )}
                  >
                    {formLoading ? 'Sending...' : 'Send Invite'}
                  </button>
                </div>
              </form>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
