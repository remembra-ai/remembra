import { useState, useEffect } from 'react';
import { User, Lock, AlertTriangle, Loader2, Shield, Smartphone, CheckCircle, XCircle } from 'lucide-react';
import clsx from 'clsx';
import { api } from '../lib/api';
import { API_V1 } from '../config';
import type { UserResponse } from '../lib/api';

type SettingsTab = 'profile' | 'password' | 'security' | 'account';

interface SettingsProps {
  onLogout: () => void;
}

export function Settings({ onLogout }: SettingsProps) {
  const [activeTab, setActiveTab] = useState<SettingsTab>('profile');
  const [user, setUser] = useState<UserResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadUserInfo();
  }, []);

  const loadUserInfo = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(`${API_V1}/auth/me`, {
        headers: {
          'Authorization': `Bearer ${api.getJwtToken()}`,
        },
      });

      if (!response.ok) {
        throw new Error('Failed to load user info');
      }

      const data = await response.json();
      setUser(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load settings');
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="w-8 h-8 animate-spin text-[#8B5CF6]" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400">
        {error}
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
          Settings
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
          Manage your account preferences and security
        </p>
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200 dark:border-gray-700 mb-6">
        <nav className="flex space-x-8">
          <button
            onClick={() => setActiveTab('profile')}
            className={clsx(
              'py-4 px-1 border-b-2 font-medium text-sm flex items-center gap-2 transition-colors',
              activeTab === 'profile'
                ? 'border-[#8B5CF6] text-[#8B5CF6] dark:text-[#A78BFA]'
                : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
            )}
          >
            <User className="w-4 h-4" />
            Profile
          </button>
          <button
            onClick={() => setActiveTab('password')}
            className={clsx(
              'py-4 px-1 border-b-2 font-medium text-sm flex items-center gap-2 transition-colors',
              activeTab === 'password'
                ? 'border-[#8B5CF6] text-[#8B5CF6] dark:text-[#A78BFA]'
                : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
            )}
          >
            <Lock className="w-4 h-4" />
            Password
          </button>
          <button
            onClick={() => setActiveTab('security')}
            className={clsx(
              'py-4 px-1 border-b-2 font-medium text-sm flex items-center gap-2 transition-colors',
              activeTab === 'security'
                ? 'border-[#8B5CF6] text-[#8B5CF6] dark:text-[#A78BFA]'
                : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
            )}
          >
            <Shield className="w-4 h-4" />
            Security
          </button>
          <button
            onClick={() => setActiveTab('account')}
            className={clsx(
              'py-4 px-1 border-b-2 font-medium text-sm flex items-center gap-2 transition-colors',
              activeTab === 'account'
                ? 'border-[#8B5CF6] text-[#8B5CF6] dark:text-[#A78BFA]'
                : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
            )}
          >
            <AlertTriangle className="w-4 h-4" />
            Account
          </button>
        </nav>
      </div>

      {/* Tab Content */}
      {activeTab === 'profile' && user && (
        <ProfileSettings user={user} onUpdate={loadUserInfo} />
      )}
      {activeTab === 'password' && (
        <PasswordSettings />
      )}
      {activeTab === 'security' && (
        <SecuritySettings />
      )}
      {activeTab === 'account' && user && (
        <AccountSettings user={user} onLogout={onLogout} />
      )}
    </div>
  );
}

// Profile Settings Component
function ProfileSettings({ user, onUpdate }: { user: UserResponse; onUpdate: () => void }) {
  const [name, setName] = useState(user.name || '');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setSuccess(false);

    try {
      await api.updateProfile(name.trim() || null);
      setSuccess(true);
      onUpdate();
      setTimeout(() => setSuccess(false), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update profile');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
        Profile Information
      </h2>

      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Email (read-only) */}
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            Email
          </label>
          <input
            type="email"
            value={user.email}
            disabled
            className="w-full px-4 py-3 rounded-lg bg-gray-100 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 cursor-not-allowed"
          />
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Email cannot be changed. Contact support if you need to update it.
          </p>
        </div>

        {/* Name */}
        <div>
          <label htmlFor="name" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            Display Name
          </label>
          <input
            id="name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Your name"
            maxLength={100}
            className="w-full px-4 py-3 rounded-lg bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#8B5CF6] focus:border-transparent"
          />
        </div>

        {/* Account Info */}
        <div className="p-3 rounded-lg bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700">
          <div className="flex items-center justify-between text-sm">
            <span className="text-gray-600 dark:text-gray-400">Account Created</span>
            <span className="text-gray-900 dark:text-white font-medium">
              {new Date(user.created_at).toLocaleDateString()}
            </span>
          </div>
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
            Profile updated successfully!
          </div>
        )}

        {/* Actions */}
        <div className="flex justify-end">
          <button
            type="submit"
            disabled={loading || name === (user.name || '')}
            className={clsx(
              'px-4 py-2 rounded-lg font-medium transition-colors',
              'bg-[#8B5CF6] hover:bg-[#7C3AED] text-white',
              (loading || name === (user.name || '')) && 'opacity-50 cursor-not-allowed'
            )}
          >
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                Saving...
              </>
            ) : (
              'Save Changes'
            )}
          </button>
        </div>
      </form>
    </div>
  );
}

// Password Settings Component
function PasswordSettings() {
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const passwordsMatch = newPassword === confirmPassword && newPassword.length > 0;
  const passwordLongEnough = newPassword.length >= 8;
  const isValid = passwordsMatch && passwordLongEnough && currentPassword.length > 0;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!isValid) {
      setError('Please fix the errors before submitting');
      return;
    }

    setLoading(true);
    setError(null);
    setSuccess(false);

    try {
      await api.changePassword(currentPassword, newPassword);
      setSuccess(true);
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
      setTimeout(() => setSuccess(false), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to change password');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
        Change Password
      </h2>

      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Current Password */}
        <div>
          <label htmlFor="current-password" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            Current Password
          </label>
          <input
            id="current-password"
            type="password"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            placeholder="Enter current password"
            className="w-full px-4 py-3 rounded-lg bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#8B5CF6] focus:border-transparent"
          />
        </div>

        {/* New Password */}
        <div>
          <label htmlFor="new-password" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            New Password
          </label>
          <input
            id="new-password"
            type="password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            placeholder="Enter new password"
            className="w-full px-4 py-3 rounded-lg bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#8B5CF6] focus:border-transparent"
          />
        </div>

        {/* Confirm Password */}
        <div>
          <label htmlFor="confirm-password" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            Confirm New Password
          </label>
          <input
            id="confirm-password"
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            placeholder="Confirm new password"
            className="w-full px-4 py-3 rounded-lg bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#8B5CF6] focus:border-transparent"
          />
        </div>

        {/* Password Requirements */}
        {(newPassword || confirmPassword) && (
          <div className="space-y-2 text-sm">
            <div className={clsx(
              'flex items-center gap-2',
              passwordLongEnough ? 'text-green-600 dark:text-green-400' : 'text-gray-400'
            )}>
              {passwordLongEnough ? '✓' : '○'} At least 8 characters
            </div>
            <div className={clsx(
              'flex items-center gap-2',
              passwordsMatch ? 'text-green-600 dark:text-green-400' : 'text-gray-400'
            )}>
              {passwordsMatch ? '✓' : '○'} Passwords match
            </div>
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="p-3 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 text-sm">
            {error}
          </div>
        )}

        {/* Success */}
        {success && (
          <div className="p-3 rounded-lg bg-green-50 dark:bg-green-900/20 text-green-600 dark:text-green-400 text-sm">
            Password changed successfully!
          </div>
        )}

        {/* Actions */}
        <div className="flex justify-end">
          <button
            type="submit"
            disabled={loading || !isValid}
            className={clsx(
              'px-4 py-2 rounded-lg font-medium transition-colors',
              'bg-[#8B5CF6] hover:bg-[#7C3AED] text-white',
              (loading || !isValid) && 'opacity-50 cursor-not-allowed'
            )}
          >
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                Changing...
              </>
            ) : (
              'Change Password'
            )}
          </button>
        </div>
      </form>
    </div>
  );
}

// Account Settings Component (Delete Account)
function AccountSettings({ user: _user, onLogout }: { user: UserResponse; onLogout: () => void }) {
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [password, setPassword] = useState('');
  const [confirmText, setConfirmText] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDelete = async () => {
    if (confirmText !== 'DELETE') {
      setError('Please type DELETE to confirm');
      return;
    }

    if (!password) {
      setError('Please enter your password');
      return;
    }

    setLoading(true);
    setError(null);

    try {
      await api.deleteAccount(password);
      // Account deleted, logout user
      onLogout();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete account');
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-red-200 dark:border-red-800 p-6">
        <div className="flex items-start gap-4">
          <div className="p-3 rounded-lg bg-red-100 dark:bg-red-900/20">
            <AlertTriangle className="w-6 h-6 text-red-600 dark:text-red-400" />
          </div>
          <div className="flex-1">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-2">
              Delete Account
            </h2>
            <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
              Permanently delete your account and all associated data. This action cannot be undone.
            </p>
            <button
              onClick={() => setShowDeleteModal(true)}
              className="px-4 py-2 rounded-lg bg-red-600 hover:bg-red-700 text-white font-medium transition-colors"
            >
              Delete Account
            </button>
          </div>
        </div>
      </div>

      {/* Delete Confirmation Modal */}
      {showDeleteModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
          <div className="w-full max-w-md bg-white dark:bg-gray-800 rounded-xl shadow-2xl">
            {/* Header */}
            <div className="p-4 border-b border-gray-200 dark:border-gray-700">
              <div className="flex items-center gap-2 text-red-600 dark:text-red-400">
                <AlertTriangle className="w-5 h-5" />
                <h2 className="text-lg font-semibold">Delete Account</h2>
              </div>
            </div>

            {/* Content */}
            <div className="p-4 space-y-4">
              <p className="text-gray-600 dark:text-gray-300">
                This will permanently delete your account and all memories. This action cannot be undone.
              </p>

              {/* Confirmation */}
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  Type <span className="font-mono text-red-600">DELETE</span> to confirm
                </label>
                <input
                  type="text"
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  placeholder="Type DELETE"
                  className="w-full px-4 py-3 rounded-lg bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-red-500 focus:border-transparent"
                />
              </div>

              {/* Password */}
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  Enter your password
                </label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Password"
                  className="w-full px-4 py-3 rounded-lg bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-red-500 focus:border-transparent"
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
                  onClick={() => {
                    setShowDeleteModal(false);
                    setPassword('');
                    setConfirmText('');
                    setError(null);
                  }}
                  className="px-4 py-2 rounded-lg bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-200 font-medium"
                  disabled={loading}
                >
                  Cancel
                </button>
                <button
                  onClick={handleDelete}
                  disabled={loading || confirmText !== 'DELETE' || !password}
                  className={clsx(
                    'px-4 py-2 rounded-lg font-medium transition-colors',
                    'bg-red-600 hover:bg-red-700 text-white',
                    (loading || confirmText !== 'DELETE' || !password) && 'opacity-50 cursor-not-allowed'
                  )}
                >
                  {loading ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                      Deleting...
                    </>
                  ) : (
                    'Delete Account'
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

// Security Settings Component (2FA/MFA)
function SecuritySettings() {
  const [totpEnabled, setTotpEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  
  // Setup state
  const [showSetup, setShowSetup] = useState(false);
  const [qrCode, setQrCode] = useState<string | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const [verifyCode, setVerifyCode] = useState('');
  
  // Disable state
  const [showDisable, setShowDisable] = useState(false);
  const [disablePassword, setDisablePassword] = useState('');

  useEffect(() => {
    checkTotpStatus();
  }, []);

  const checkTotpStatus = async () => {
    setLoading(true);
    try {
      const response = await fetch(`${API_V1}/auth/2fa/status`, {
        headers: {
          'Authorization': `Bearer ${api.getJwtToken()}`,
        },
      });
      if (response.ok) {
        const data = await response.json();
        setTotpEnabled(data.enabled);
      }
    } catch (err) {
      console.error('Failed to check 2FA status:', err);
    } finally {
      setLoading(false);
    }
  };

  const startSetup = async () => {
    setActionLoading(true);
    setError(null);
    try {
      const response = await fetch(`${API_V1}/auth/2fa/setup`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${api.getJwtToken()}`,
          'Content-Type': 'application/json',
        },
      });
      
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to start 2FA setup');
      }
      
      const data = await response.json();
      setQrCode(data.qr_code);
      setSecret(data.secret);
      setShowSetup(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start 2FA setup');
    } finally {
      setActionLoading(false);
    }
  };

  const enableTotp = async () => {
    if (verifyCode.length !== 6) {
      setError('Please enter a 6-digit code');
      return;
    }
    
    setActionLoading(true);
    setError(null);
    try {
      const response = await fetch(`${API_V1}/auth/2fa/enable`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${api.getJwtToken()}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ code: verifyCode }),
      });
      
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Invalid verification code');
      }
      
      setTotpEnabled(true);
      setShowSetup(false);
      setQrCode(null);
      setSecret(null);
      setVerifyCode('');
      setSuccess('Two-factor authentication enabled successfully!');
      setTimeout(() => setSuccess(null), 5000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to enable 2FA');
    } finally {
      setActionLoading(false);
    }
  };

  const disableTotp = async () => {
    if (!disablePassword) {
      setError('Please enter your password');
      return;
    }
    
    setActionLoading(true);
    setError(null);
    try {
      const response = await fetch(`${API_V1}/auth/2fa/disable`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${api.getJwtToken()}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ password: disablePassword }),
      });
      
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to disable 2FA');
      }
      
      setTotpEnabled(false);
      setShowDisable(false);
      setDisablePassword('');
      setSuccess('Two-factor authentication disabled');
      setTimeout(() => setSuccess(null), 5000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to disable 2FA');
    } finally {
      setActionLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="w-6 h-6 animate-spin text-[#8B5CF6]" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Success Message */}
      {success && (
        <div className="p-4 rounded-lg bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-400 flex items-center gap-2">
          <CheckCircle className="w-5 h-5" />
          {success}
        </div>
      )}

      {/* Error Message */}
      {error && (
        <div className="p-4 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 flex items-center gap-2">
          <XCircle className="w-5 h-5" />
          {error}
        </div>
      )}

      {/* 2FA Card */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6">
        <div className="flex items-start justify-between">
          <div className="flex items-start gap-4">
            <div className={clsx(
              'p-3 rounded-lg',
              totpEnabled ? 'bg-green-100 dark:bg-green-900/30' : 'bg-gray-100 dark:bg-gray-700'
            )}>
              <Smartphone className={clsx(
                'w-6 h-6',
                totpEnabled ? 'text-green-600 dark:text-green-400' : 'text-gray-500 dark:text-gray-400'
              )} />
            </div>
            <div>
              <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                Two-Factor Authentication
              </h3>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                Add an extra layer of security to your account using an authenticator app.
              </p>
              <div className={clsx(
                'inline-flex items-center gap-1.5 mt-3 px-2.5 py-1 rounded-full text-xs font-medium',
                totpEnabled
                  ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400'
                  : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
              )}>
                {totpEnabled ? (
                  <>
                    <CheckCircle className="w-3.5 h-3.5" />
                    Enabled
                  </>
                ) : (
                  <>
                    <XCircle className="w-3.5 h-3.5" />
                    Not enabled
                  </>
                )}
              </div>
            </div>
          </div>
          
          {!showSetup && !showDisable && (
            <button
              onClick={totpEnabled ? () => setShowDisable(true) : startSetup}
              disabled={actionLoading}
              className={clsx(
                'px-4 py-2 rounded-lg font-medium text-sm transition-colors',
                totpEnabled
                  ? 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400 hover:bg-red-200 dark:hover:bg-red-900/50'
                  : 'bg-[#8B5CF6] hover:bg-[#7C3AED] text-white',
                actionLoading && 'opacity-50 cursor-not-allowed'
              )}
            >
              {actionLoading ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : totpEnabled ? (
                'Disable'
              ) : (
                'Enable'
              )}
            </button>
          )}
        </div>

        {/* Setup Flow */}
        {showSetup && (
          <div className="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700">
            <h4 className="text-sm font-semibold text-gray-900 dark:text-white mb-4">
              Set up authenticator app
            </h4>
            
            <div className="space-y-4">
              <p className="text-sm text-gray-600 dark:text-gray-400">
                1. Scan this QR code with your authenticator app (Google Authenticator, Authy, etc.)
              </p>
              
              {qrCode && (
                <div className="flex justify-center p-4 bg-white rounded-lg">
                  <img src={qrCode} alt="2FA QR Code" className="w-48 h-48" />
                </div>
              )}
              
              {secret && (
                <div className="text-center">
                  <p className="text-xs text-gray-500 dark:text-gray-400 mb-1">
                    Or enter this code manually:
                  </p>
                  <code className="px-3 py-1.5 bg-gray-100 dark:bg-gray-900 rounded text-sm font-mono text-gray-800 dark:text-gray-200">
                    {secret}
                  </code>
                </div>
              )}
              
              <p className="text-sm text-gray-600 dark:text-gray-400">
                2. Enter the 6-digit code from your app to verify:
              </p>
              
              <input
                type="text"
                value={verifyCode}
                onChange={(e) => setVerifyCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                placeholder="000000"
                maxLength={6}
                className="w-full px-4 py-3 rounded-lg bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 text-gray-900 dark:text-white text-center text-2xl font-mono tracking-widest placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#8B5CF6] focus:border-transparent"
              />
              
              <div className="flex gap-3">
                <button
                  onClick={() => {
                    setShowSetup(false);
                    setQrCode(null);
                    setSecret(null);
                    setVerifyCode('');
                    setError(null);
                  }}
                  className="flex-1 px-4 py-2 rounded-lg bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-200 font-medium"
                >
                  Cancel
                </button>
                <button
                  onClick={enableTotp}
                  disabled={actionLoading || verifyCode.length !== 6}
                  className={clsx(
                    'flex-1 px-4 py-2 rounded-lg font-medium transition-colors',
                    'bg-[#8B5CF6] hover:bg-[#7C3AED] text-white',
                    (actionLoading || verifyCode.length !== 6) && 'opacity-50 cursor-not-allowed'
                  )}
                >
                  {actionLoading ? (
                    <Loader2 className="w-4 h-4 animate-spin mx-auto" />
                  ) : (
                    'Verify & Enable'
                  )}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Disable Flow */}
        {showDisable && (
          <div className="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700">
            <h4 className="text-sm font-semibold text-gray-900 dark:text-white mb-4">
              Disable two-factor authentication
            </h4>
            
            <div className="space-y-4">
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Enter your password to disable 2FA. This will make your account less secure.
              </p>
              
              <input
                type="password"
                value={disablePassword}
                onChange={(e) => setDisablePassword(e.target.value)}
                placeholder="Enter your password"
                className="w-full px-4 py-3 rounded-lg bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-red-500 focus:border-transparent"
              />
              
              <div className="flex gap-3">
                <button
                  onClick={() => {
                    setShowDisable(false);
                    setDisablePassword('');
                    setError(null);
                  }}
                  className="flex-1 px-4 py-2 rounded-lg bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-200 font-medium"
                >
                  Cancel
                </button>
                <button
                  onClick={disableTotp}
                  disabled={actionLoading || !disablePassword}
                  className={clsx(
                    'flex-1 px-4 py-2 rounded-lg font-medium transition-colors',
                    'bg-red-600 hover:bg-red-700 text-white',
                    (actionLoading || !disablePassword) && 'opacity-50 cursor-not-allowed'
                  )}
                >
                  {actionLoading ? (
                    <Loader2 className="w-4 h-4 animate-spin mx-auto" />
                  ) : (
                    'Disable 2FA'
                  )}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Security Tips */}
      <div className="bg-blue-50 dark:bg-blue-900/20 rounded-xl border border-blue-100 dark:border-blue-800 p-4">
        <h4 className="text-sm font-semibold text-blue-800 dark:text-blue-300 mb-2">
          Security Tips
        </h4>
        <ul className="text-sm text-blue-700 dark:text-blue-400 space-y-1">
          <li>• Use a strong, unique password for your account</li>
          <li>• Enable 2FA for an extra layer of protection</li>
          <li>• Never share your API keys or passwords</li>
          <li>• Regularly review your active sessions</li>
        </ul>
      </div>
    </div>
  );
}
