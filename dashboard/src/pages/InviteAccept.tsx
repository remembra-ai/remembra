import { useState, useEffect } from 'react';
import { Users, Check, Loader2 } from 'lucide-react';
import clsx from 'clsx';
import { API_V1 } from '../config';

interface InviteAcceptProps {
  token: string;
  isAuthenticated: boolean;
  onAccepted: (teamId: string, teamName: string) => void;
  onSwitchToLogin: () => void;
  onSwitchToSignup: () => void;
}

export function InviteAccept({ 
  token, 
  isAuthenticated, 
  onAccepted,
  onSwitchToLogin,
  onSwitchToSignup 
}: InviteAcceptProps) {
  const [loading, setLoading] = useState(true);
  const [accepting, setAccepting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState(false);

  useEffect(() => {
    fetchInviteDetails();
  }, [token]);

  const getAuthHeaders = (): Record<string, string> => {
    const jwtToken = localStorage.getItem('remembra_jwt_token');
    if (jwtToken) {
      return { 'Authorization': `Bearer ${jwtToken}` };
    }
    return {};
  };

  const fetchInviteDetails = async () => {
    try {
      setLoading(true);
      // We need a public endpoint to get invite details by token
      // For now, we'll just show a generic accept page
      // The actual validation happens on accept
      setLoading(false);
    } catch {
      setError('Failed to load invite details');
      setLoading(false);
    }
  };

  const acceptInvite = async () => {
    if (!isAuthenticated) {
      // Store token for after login
      localStorage.setItem('pending_invite_token', token);
      onSwitchToLogin();
      return;
    }

    setAccepting(true);
    setError(null);

    try {
      const response = await fetch(`${API_V1}/teams/invites/accept`, {
        method: 'POST',
        headers: {
          ...getAuthHeaders(),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ token }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to accept invite');
      }

      const result = await response.json();
      setAccepted(true);
      
      // Clear pending token
      localStorage.removeItem('pending_invite_token');
      
      // Notify parent
      setTimeout(() => {
        onAccepted(result.team_id, result.team_name);
      }, 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to accept invite');
    } finally {
      setAccepting(false);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-[hsl(var(--background))] flex items-center justify-center">
        <Loader2 className="w-8 h-8 animate-spin text-signal-ink" />
      </div>
    );
  }

  if (accepted) {
    return (
      <div className="min-h-screen bg-[hsl(var(--background))] flex items-center justify-center p-4">
        <div className="max-w-md w-full bg-[hsl(var(--card))] rounded-2xl p-8 border border-[hsl(var(--border))] text-center">
          <div className="w-16 h-16 mx-auto mb-6 rounded-full bg-green-500/20 flex items-center justify-center">
            <Check className="w-8 h-8 text-green-500" />
          </div>
          <h1 className="text-2xl font-bold text-[hsl(var(--foreground))] mb-2">
            Welcome to the team!
          </h1>
          <p className="text-[hsl(var(--muted-foreground))]">
            Redirecting to your dashboard...
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[hsl(var(--background))] flex items-center justify-center p-4">
      <div className="max-w-md w-full bg-[hsl(var(--card))] rounded-2xl p-8 border border-[hsl(var(--border))]">
        {/* Header */}
        <div className="text-center mb-8">
          <div className="w-16 h-16 mx-auto mb-6 rounded-full bg-signal/20 flex items-center justify-center">
            <Users className="w-8 h-8 text-signal-ink" />
          </div>
          <h1 className="text-2xl font-bold text-[hsl(var(--foreground))] mb-2">
            You're Invited!
          </h1>
          <p className="text-[hsl(var(--muted-foreground))]">
            You've been invited to join a team on Remembra
          </p>
        </div>

        {error && (
          <div className="mb-6 p-4 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
            {error}
          </div>
        )}

        {/* Actions */}
        <div className="space-y-4">
          {isAuthenticated ? (
            <button
              onClick={acceptInvite}
              disabled={accepting}
              className={clsx(
                'w-full py-3 rounded-lg font-semibold text-white',
                'bg-accent hover:bg-accent-hover',
                'transition-colors',
                'disabled:opacity-50 disabled:cursor-not-allowed',
                'flex items-center justify-center gap-2'
              )}
            >
              {accepting ? (
                <>
                  <Loader2 className="w-5 h-5 animate-spin" />
                  Joining...
                </>
              ) : (
                <>
                  <Check className="w-5 h-5" />
                  Accept Invitation
                </>
              )}
            </button>
          ) : (
            <>
              <p className="text-center text-[hsl(var(--muted-foreground))] text-sm mb-4">
                Sign in or create an account to accept this invitation
              </p>
              
              <button
                onClick={() => {
                  localStorage.setItem('pending_invite_token', token);
                  onSwitchToLogin();
                }}
                className={clsx(
                  'w-full py-3 rounded-lg font-semibold text-white',
                  'bg-accent hover:bg-accent-hover',
                  'transition-colors'
                )}
              >
                Sign In
              </button>
              
              <button
                onClick={() => {
                  localStorage.setItem('pending_invite_token', token);
                  onSwitchToSignup();
                }}
                className={clsx(
                  'w-full py-3 rounded-lg font-semibold',
                  'bg-[hsl(var(--muted))] hover:bg-[hsl(var(--muted))]/80',
                  'text-[hsl(var(--foreground))]',
                  'transition-colors'
                )}
              >
                Create Account
              </button>
            </>
          )}
        </div>

        {/* Footer */}
        <p className="mt-6 text-center text-xs text-[hsl(var(--muted-foreground))]">
          By accepting, you'll be able to access shared memory spaces with your team.
        </p>
      </div>
    </div>
  );
}
