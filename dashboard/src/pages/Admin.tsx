import { useState, useEffect } from 'react';
import { API_V1 } from '../config';

interface User {
  id: string;
  email: string;
  name: string | null;
  plan: string;
  memories_count: number;
  api_keys_count: number;
  created_at: string;
  last_login_at: string | null;
  is_active: boolean;
}

interface UserDetail {
  id: string;
  email: string;
  name: string | null;
  plan: string;
  stripe_customer_id: string | null;
  stripe_subscription_id: string | null;
  created_at: string;
  last_login_at: string | null;
  is_active: boolean;
  email_verified: boolean;
  totp_enabled: boolean;
  usage: {
    memories_stored: number;
    recalls_this_month: number;
    stores_this_month: number;
    api_keys_active: number;
  };
  limits: {
    max_memories: number;
    max_recalls_per_month: number;
    max_stores_per_month: number;
    max_api_keys: number;
    has_webhooks: boolean;
    has_priority_support: boolean;
  };
}

interface PlatformStats {
  users: {
    total: number;
    active: number;
    recent_signups_7d: number;
  };
  memories: {
    total: number;
  };
  api_keys: {
    active: number;
  };
  plans: Record<string, number>;
}

const PLAN_COLORS: Record<string, string> = {
  free: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300',
  pro: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-300',
  team: 'bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-300',
  enterprise: 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-300',
};

export function Admin() {
  const [users, setUsers] = useState<User[]>([]);
  const [stats, setStats] = useState<PlatformStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [planFilter, setPlanFilter] = useState<string>('');
  const [selectedUser, setSelectedUser] = useState<UserDetail | null>(null);
  const [showUserModal, setShowUserModal] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);

  const getAuthHeaders = () => {
    const token = localStorage.getItem('remembra_jwt_token');
    return {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json',
    };
  };

  const fetchUsers = async () => {
    try {
      const params = new URLSearchParams();
      if (searchQuery) params.append('search', searchQuery);
      if (planFilter) params.append('plan', planFilter);
      params.append('limit', '100');

      const response = await fetch(`${API_V1}/admin/users?${params}`, {
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        if (response.status === 403) {
          throw new Error('Access denied. Superadmin privileges required.');
        }
        throw new Error('Failed to fetch users');
      }

      const data = await response.json();
      setUsers(data.users);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    }
  };

  const fetchStats = async () => {
    try {
      const response = await fetch(`${API_V1}/admin/stats`, {
        headers: getAuthHeaders(),
      });

      if (response.ok) {
        const data = await response.json();
        setStats(data);
      }
    } catch (err) {
      console.error('Failed to fetch stats:', err);
    }
  };

  const fetchUserDetail = async (userId: string) => {
    try {
      // Clear previous action messages
      setActionError(null);
      setActionSuccess(null);
      
      const response = await fetch(`${API_V1}/admin/users/${userId}`, {
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        throw new Error('Failed to fetch user details');
      }

      const data = await response.json();
      setSelectedUser(data);
      setShowUserModal(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    }
  };

  const updateUserTier = async (userId: string, newPlan: string) => {
    setActionLoading(true);
    try {
      const response = await fetch(`${API_V1}/admin/users/${userId}/tier`, {
        method: 'PATCH',
        headers: getAuthHeaders(),
        body: JSON.stringify({ plan: newPlan }),
      });

      if (!response.ok) {
        throw new Error('Failed to update user tier');
      }

      await fetchUsers();
      if (selectedUser) {
        await fetchUserDetail(userId);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setActionLoading(false);
    }
  };

  const resetUserPassword = async (userId: string) => {
    if (!confirm('Reset this user\'s password? They will receive a temporary password.')) {
      return;
    }

    setActionLoading(true);
    setActionError(null);
    setActionSuccess(null);
    try {
      const response = await fetch(`${API_V1}/admin/users/${userId}/reset-password`, {
        method: 'POST',
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `Failed to reset password (${response.status})`);
      }

      const data = await response.json();
      setActionSuccess(`Password reset! Temporary password: ${data.temporary_password}`);
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Unknown error';
      setActionError(errorMsg);
      console.error('Reset password error:', err);
    } finally {
      setActionLoading(false);
    }
  };

  const toggleUserActive = async (userId: string, active: boolean) => {
    const action = active ? 'activate' : 'deactivate';
    if (!confirm(`${active ? 'Activate' : 'Deactivate'} this user account?`)) {
      return;
    }

    setActionLoading(true);
    setActionError(null);
    setActionSuccess(null);
    try {
      const response = await fetch(`${API_V1}/admin/users/${userId}/activate?active=${active}`, {
        method: 'POST',
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `Failed to ${action} user (${response.status})`);
      }

      setActionSuccess(`User ${action}d successfully`);
      await fetchUsers();
      if (selectedUser) {
        await fetchUserDetail(userId);
      }
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Unknown error';
      setActionError(errorMsg);
      console.error('Toggle user active error:', err);
    } finally {
      setActionLoading(false);
    }
  };

  const deleteUser = async (userId: string, email: string) => {
    const confirmText = prompt(`Type "${email}" to confirm permanent deletion:`);
    if (confirmText !== email) {
      setActionError('Deletion cancelled - email did not match');
      return;
    }

    setActionLoading(true);
    setActionError(null);
    setActionSuccess(null);
    try {
      const response = await fetch(`${API_V1}/admin/users/${userId}?confirm=true`, {
        method: 'DELETE',
        headers: getAuthHeaders(),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `Failed to delete user (${response.status})`);
      }

      setShowUserModal(false);
      setSelectedUser(null);
      setActionSuccess(`User ${email} deleted successfully`);
      await fetchUsers();
      await fetchStats();
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Unknown error';
      setActionError(errorMsg);
      console.error('Delete user error:', err);
    } finally {
      setActionLoading(false);
    }
  };

  useEffect(() => {
    const loadData = async () => {
      setLoading(true);
      await Promise.all([fetchUsers(), fetchStats()]);
      setLoading(false);
    };
    loadData();
  }, []);

  useEffect(() => {
    const debounce = setTimeout(() => {
      fetchUsers();
    }, 300);
    return () => clearTimeout(debounce);
  }, [searchQuery, planFilter]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-purple-500"></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
          <h3 className="text-red-800 dark:text-red-200 font-semibold">Access Denied</h3>
          <p className="text-red-600 dark:text-red-400 mt-1">{error}</p>
          <p className="text-red-500 dark:text-red-500 mt-2 text-sm">
            Admin access requires your email to be in REMEMBRA_OWNER_EMAILS.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Admin Dashboard</h1>
          <p className="text-gray-500 dark:text-gray-400">Manage users, plans, and platform operations</p>
        </div>
      </div>

      {/* Stats Cards */}
      {stats && (
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow-sm border border-gray-200 dark:border-gray-700">
            <div className="text-sm text-gray-500 dark:text-gray-400">Total Users</div>
            <div className="text-2xl font-bold text-gray-900 dark:text-white">{stats.users.total}</div>
            <div className="text-xs text-green-600 dark:text-green-400">+{stats.users.recent_signups_7d} this week</div>
          </div>
          <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow-sm border border-gray-200 dark:border-gray-700">
            <div className="text-sm text-gray-500 dark:text-gray-400">Active Users</div>
            <div className="text-2xl font-bold text-gray-900 dark:text-white">{stats.users.active}</div>
          </div>
          <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow-sm border border-gray-200 dark:border-gray-700">
            <div className="text-sm text-gray-500 dark:text-gray-400">Total Memories</div>
            <div className="text-2xl font-bold text-gray-900 dark:text-white">{stats.memories.total.toLocaleString()}</div>
          </div>
          <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow-sm border border-gray-200 dark:border-gray-700">
            <div className="text-sm text-gray-500 dark:text-gray-400">Active API Keys</div>
            <div className="text-2xl font-bold text-gray-900 dark:text-white">{stats.api_keys.active}</div>
          </div>
        </div>
      )}

      {/* Plan Distribution */}
      {stats && (
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow-sm border border-gray-200 dark:border-gray-700">
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">Plan Distribution</h3>
          <div className="flex gap-4 flex-wrap">
            {Object.entries(stats.plans).map(([plan, count]) => (
              <div key={plan} className="flex items-center gap-2">
                <span className={`px-2 py-1 rounded text-xs font-medium ${PLAN_COLORS[plan] || PLAN_COLORS.free}`}>
                  {plan.toUpperCase()}
                </span>
                <span className="text-gray-600 dark:text-gray-400">{count} users</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Search & Filter */}
      <div className="flex gap-4 flex-wrap">
        <input
          type="text"
          placeholder="Search by email or name..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="flex-1 min-w-[200px] px-4 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-white focus:ring-2 focus:ring-purple-500 focus:border-transparent"
        />
        <select
          value={planFilter}
          onChange={(e) => setPlanFilter(e.target.value)}
          className="px-4 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-white focus:ring-2 focus:ring-purple-500 focus:border-transparent"
        >
          <option value="">All Plans</option>
          <option value="free">Free</option>
          <option value="pro">Pro</option>
          <option value="team">Team</option>
          <option value="enterprise">Enterprise</option>
        </select>
      </div>

      {/* Users Table */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
          <thead className="bg-gray-50 dark:bg-gray-900">
            <tr>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">User</th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">Plan</th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">Memories</th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">API Keys</th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">Status</th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">Created</th>
              <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">Actions</th>
            </tr>
          </thead>
          <tbody className="bg-white dark:bg-gray-800 divide-y divide-gray-200 dark:divide-gray-700">
            {users.map((user) => (
              <tr key={user.id} className="hover:bg-gray-50 dark:hover:bg-gray-700">
                <td className="px-6 py-4 whitespace-nowrap">
                  <div className="text-sm font-medium text-gray-900 dark:text-white">{user.email}</div>
                  {user.name && <div className="text-sm text-gray-500 dark:text-gray-400">{user.name}</div>}
                </td>
                <td className="px-6 py-4 whitespace-nowrap">
                  <span className={`px-2 py-1 rounded text-xs font-medium ${PLAN_COLORS[user.plan] || PLAN_COLORS.free}`}>
                    {user.plan.toUpperCase()}
                  </span>
                </td>
                <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                  {user.memories_count.toLocaleString()}
                </td>
                <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                  {user.api_keys_count}
                </td>
                <td className="px-6 py-4 whitespace-nowrap">
                  {user.is_active ? (
                    <span className="px-2 py-1 rounded text-xs font-medium bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-300">
                      Active
                    </span>
                  ) : (
                    <span className="px-2 py-1 rounded text-xs font-medium bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-300">
                      Inactive
                    </span>
                  )}
                </td>
                <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                  {new Date(user.created_at).toLocaleDateString()}
                </td>
                <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                  <button
                    onClick={() => fetchUserDetail(user.id)}
                    className="text-purple-600 hover:text-purple-900 dark:text-purple-400 dark:hover:text-purple-300"
                  >
                    Manage
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        
        {users.length === 0 && (
          <div className="text-center py-8 text-gray-500 dark:text-gray-400">
            No users found
          </div>
        )}
      </div>

      {/* User Detail Modal */}
      {showUserModal && selectedUser && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
            <div className="p-6 border-b border-gray-200 dark:border-gray-700">
              <div className="flex items-center justify-between">
                <h2 className="text-xl font-bold text-gray-900 dark:text-white">User Details</h2>
                <button
                  onClick={() => setShowUserModal(false)}
                  className="text-gray-400 hover:text-gray-500"
                >
                  <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>

            <div className="p-6 space-y-6">
              {/* User Info */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="text-sm text-gray-500 dark:text-gray-400">Email</label>
                  <div className="font-medium text-gray-900 dark:text-white">{selectedUser.email}</div>
                </div>
                <div>
                  <label className="text-sm text-gray-500 dark:text-gray-400">Name</label>
                  <div className="font-medium text-gray-900 dark:text-white">{selectedUser.name || '-'}</div>
                </div>
                <div>
                  <label className="text-sm text-gray-500 dark:text-gray-400">User ID</label>
                  <div className="font-mono text-sm text-gray-600 dark:text-gray-400">{selectedUser.id}</div>
                </div>
                <div>
                  <label className="text-sm text-gray-500 dark:text-gray-400">Created</label>
                  <div className="text-gray-900 dark:text-white">{new Date(selectedUser.created_at).toLocaleString()}</div>
                </div>
              </div>

              {/* Plan Management */}
              <div className="border-t border-gray-200 dark:border-gray-700 pt-4">
                <h3 className="font-medium text-gray-900 dark:text-white mb-3">Plan Management</h3>
                <div className="flex items-center gap-4">
                  <span className={`px-3 py-1 rounded font-medium ${PLAN_COLORS[selectedUser.plan] || PLAN_COLORS.free}`}>
                    Current: {selectedUser.plan.toUpperCase()}
                  </span>
                  <select
                    onChange={(e) => updateUserTier(selectedUser.id, e.target.value)}
                    disabled={actionLoading}
                    className="px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                    defaultValue=""
                  >
                    <option value="" disabled>Change plan...</option>
                    <option value="free">Free</option>
                    <option value="pro">Pro ($49/mo)</option>
                    <option value="team">Team ($199/mo)</option>
                    <option value="enterprise">Enterprise</option>
                  </select>
                </div>
              </div>

              {/* Usage Stats */}
              <div className="border-t border-gray-200 dark:border-gray-700 pt-4">
                <h3 className="font-medium text-gray-900 dark:text-white mb-3">Usage</h3>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                  <div className="bg-gray-50 dark:bg-gray-700 rounded p-3">
                    <div className="text-sm text-gray-500 dark:text-gray-400">Memories</div>
                    <div className="text-lg font-bold text-gray-900 dark:text-white">
                      {selectedUser.usage.memories_stored.toLocaleString()} / {selectedUser.limits.max_memories.toLocaleString()}
                    </div>
                  </div>
                  <div className="bg-gray-50 dark:bg-gray-700 rounded p-3">
                    <div className="text-sm text-gray-500 dark:text-gray-400">Recalls/mo</div>
                    <div className="text-lg font-bold text-gray-900 dark:text-white">
                      {selectedUser.usage.recalls_this_month.toLocaleString()}
                    </div>
                  </div>
                  <div className="bg-gray-50 dark:bg-gray-700 rounded p-3">
                    <div className="text-sm text-gray-500 dark:text-gray-400">Stores/mo</div>
                    <div className="text-lg font-bold text-gray-900 dark:text-white">
                      {selectedUser.usage.stores_this_month.toLocaleString()}
                    </div>
                  </div>
                  <div className="bg-gray-50 dark:bg-gray-700 rounded p-3">
                    <div className="text-sm text-gray-500 dark:text-gray-400">API Keys</div>
                    <div className="text-lg font-bold text-gray-900 dark:text-white">
                      {selectedUser.usage.api_keys_active} / {selectedUser.limits.max_api_keys}
                    </div>
                  </div>
                </div>
              </div>

              {/* Account Status */}
              <div className="border-t border-gray-200 dark:border-gray-700 pt-4">
                <h3 className="font-medium text-gray-900 dark:text-white mb-3">Account Status</h3>
                <div className="flex flex-wrap gap-2">
                  <span className={`px-2 py-1 rounded text-xs font-medium ${selectedUser.is_active ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-300' : 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-300'}`}>
                    {selectedUser.is_active ? 'Active' : 'Inactive'}
                  </span>
                  <span className={`px-2 py-1 rounded text-xs font-medium ${selectedUser.email_verified ? 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-300' : 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300'}`}>
                    {selectedUser.email_verified ? 'Email Verified' : 'Email Not Verified'}
                  </span>
                  {selectedUser.totp_enabled && (
                    <span className="px-2 py-1 rounded text-xs font-medium bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-300">
                      2FA Enabled
                    </span>
                  )}
                </div>
              </div>

              {/* Stripe Info */}
              {(selectedUser.stripe_customer_id || selectedUser.stripe_subscription_id) && (
                <div className="border-t border-gray-200 dark:border-gray-700 pt-4">
                  <h3 className="font-medium text-gray-900 dark:text-white mb-3">Stripe</h3>
                  <div className="text-sm text-gray-600 dark:text-gray-400 space-y-1">
                    {selectedUser.stripe_customer_id && <div>Customer: {selectedUser.stripe_customer_id}</div>}
                    {selectedUser.stripe_subscription_id && <div>Subscription: {selectedUser.stripe_subscription_id}</div>}
                  </div>
                </div>
              )}

              {/* Actions */}
              <div className="border-t border-gray-200 dark:border-gray-700 pt-4">
                <h3 className="font-medium text-gray-900 dark:text-white mb-3">Actions</h3>
                
                {/* Action feedback messages */}
                {actionError && (
                  <div className="mb-3 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg">
                    <p className="text-sm text-red-600 dark:text-red-400">{actionError}</p>
                  </div>
                )}
                {actionSuccess && (
                  <div className="mb-3 p-3 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg">
                    <p className="text-sm text-green-600 dark:text-green-400">{actionSuccess}</p>
                  </div>
                )}
                
                <div className="flex flex-wrap gap-3">
                  <button
                    onClick={() => resetUserPassword(selectedUser.id)}
                    disabled={actionLoading}
                    className="px-4 py-2 bg-yellow-500 hover:bg-yellow-600 text-white rounded-lg font-medium disabled:opacity-50 flex items-center gap-2"
                  >
                    {actionLoading && <span className="animate-spin">⏳</span>}
                    Reset Password
                  </button>
                  <button
                    onClick={() => toggleUserActive(selectedUser.id, !selectedUser.is_active)}
                    disabled={actionLoading}
                    className={`px-4 py-2 rounded-lg font-medium disabled:opacity-50 flex items-center gap-2 ${
                      selectedUser.is_active
                        ? 'bg-orange-500 hover:bg-orange-600 text-white'
                        : 'bg-green-500 hover:bg-green-600 text-white'
                    }`}
                  >
                    {actionLoading && <span className="animate-spin">⏳</span>}
                    {selectedUser.is_active ? 'Deactivate' : 'Activate'}
                  </button>
                  <button
                    onClick={() => deleteUser(selectedUser.id, selectedUser.email)}
                    disabled={actionLoading}
                    className="px-4 py-2 bg-red-500 hover:bg-red-600 text-white rounded-lg font-medium disabled:opacity-50 flex items-center gap-2"
                  >
                    {actionLoading && <span className="animate-spin">⏳</span>}
                    Delete User
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
