import { useCallback, useState } from 'react';
import { UserPlus, Loader2, Eye, EyeOff, Check, X } from 'lucide-react';
import { API_V1 } from '../config';
import { BrandLockup } from '../brand/Brand';
import { SocialSignIn } from '../components/auth/SocialSignIn';
import { TurnstileWidget } from '../components/auth/TurnstileWidget';
import { useAuthConfig } from '../hooks/useAuthConfig';
import { passwordChecks as checkPassword } from '../lib/authProviders';

interface SignupProps {
  onSignup: (user: { id: string; email: string; name?: string }) => void;
  onSwitchToLogin: () => void;
}

function detailMessage(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail;
  // FastAPI validation errors: [{ msg: "Value error, Password must contain: ..." }]
  if (Array.isArray(detail) && detail[0] && typeof detail[0].msg === 'string') {
    return detail[0].msg.replace(/^Value error, /, '');
  }
  return fallback;
}

export function Signup({ onSignup, onSwitchToLogin }: SignupProps) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [name, setName] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null);
  const [turnstileReset, setTurnstileReset] = useState(0);
  const { config, loading: configLoading } = useAuthConfig();
  const siteKey = config.turnstile_site_key;
  const dark = typeof document !== 'undefined' && document.documentElement.classList.contains('dark');

  const rules = checkPassword(password);
  const passwordMatch = password === confirmPassword && password.length > 0;
  const isPasswordValid = Object.values(rules).every(Boolean) && passwordMatch;
  const needsHumanCheck = !!siteKey && !turnstileToken;

  const onTurnstileToken = useCallback((token: string | null) => setTurnstileToken(token), []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!email.trim()) {
      setError('Please enter your email');
      return;
    }

    if (!isPasswordValid) {
      setError('Please fix the password requirements');
      return;
    }

    if (needsHumanCheck) {
      setError('Please complete the human check');
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const response = await fetch(`${API_V1}/auth/signup`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          email,
          password,
          name: name.trim() || undefined,
          turnstile_token: turnstileToken || undefined,
        }),
      });

      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        if (response.status === 429) throw new Error(detailMessage(data.detail, 'Too many signups. Please try again later.'));
        throw new Error(detailMessage(data.detail, 'Signup failed'));
      }

      onSignup({ id: data.id, email: data.email, name: data.name });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Signup failed');
      // Turnstile tokens are single use: get a fresh one for the retry.
      if (siteKey) setTurnstileReset((n) => n + 1);
    } finally {
      setLoading(false);
    }
  };

  const PasswordCheck = ({ valid, text }: { valid: boolean; text: string }) => (
    <div className={`flex items-center gap-2 text-sm ${valid ? 'text-green-400' : 'text-[hsl(var(--muted-foreground))]'}`}>
      {valid ? <Check className="w-4 h-4" aria-hidden="true" /> : <X className="w-4 h-4" aria-hidden="true" />}
      <span>{text}</span>
    </div>
  );

  return (
    <div className="min-h-screen flex items-center justify-center bg-[hsl(var(--background))] px-4 py-8">
      <div className="max-w-md w-full">
        <div className="text-center mb-8">
          <div className="mb-6 flex items-center justify-center lg:hidden">
            <BrandLockup height={38} className="text-ink" />
          </div>
          <h1 className="font-display text-3xl font-extrabold tracking-[-0.03em] text-[hsl(var(--foreground))]">
            Create your account
          </h1>
          <p className="text-[hsl(var(--muted-foreground))] mt-2">
            Get started with Remembra for free
          </p>
        </div>

        <form onSubmit={handleSubmit} className="bg-[hsl(var(--card))] rounded-xl shadow-sm border border-[hsl(var(--border))] p-6">
          <div className="space-y-4">
            <SocialSignIn
              providers={config.providers}
              from="signup"
              disabled={loading}
              dividerLabel="or sign up with email"
            />
            <div>
              <label htmlFor="name" className="block text-sm font-medium text-[hsl(var(--foreground))] mb-2">
                Name <span className="text-[hsl(var(--muted-foreground))]">(optional)</span>
              </label>
              <input
                id="name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Your name"
                autoComplete="name"
                maxLength={100}
                className="w-full px-4 py-3 rounded-lg bg-[hsl(var(--input))] border border-[hsl(var(--border))] text-[hsl(var(--foreground))] placeholder-[hsl(var(--muted-foreground))] focus:outline-none focus:ring-2 focus:ring-signal focus:border-transparent"
              />
            </div>

            <div>
              <label htmlFor="email" className="block text-sm font-medium text-[hsl(var(--foreground))] mb-2">
                Email
              </label>
              <input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                autoComplete="email"
                required
                className="w-full px-4 py-3 rounded-lg bg-[hsl(var(--input))] border border-[hsl(var(--border))] text-[hsl(var(--foreground))] placeholder-[hsl(var(--muted-foreground))] focus:outline-none focus:ring-2 focus:ring-signal focus:border-transparent"
              />
            </div>

            <div>
              <label htmlFor="password" className="block text-sm font-medium text-[hsl(var(--foreground))] mb-2">
                Password
              </label>
              <div className="relative">
                <input
                  id="password"
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  autoComplete="new-password"
                  required
                  aria-describedby="password-rules"
                  className="w-full px-4 py-3 rounded-lg bg-[hsl(var(--input))] border border-[hsl(var(--border))] text-[hsl(var(--foreground))] placeholder-[hsl(var(--muted-foreground))] focus:outline-none focus:ring-2 focus:ring-signal focus:border-transparent pr-12"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
                >
                  {showPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                </button>
              </div>
            </div>

            <div>
              <label htmlFor="confirmPassword" className="block text-sm font-medium text-[hsl(var(--foreground))] mb-2">
                Confirm Password
              </label>
              <input
                id="confirmPassword"
                type={showPassword ? 'text' : 'password'}
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder="••••••••"
                autoComplete="new-password"
                required
                className="w-full px-4 py-3 rounded-lg bg-[hsl(var(--input))] border border-[hsl(var(--border))] text-[hsl(var(--foreground))] placeholder-[hsl(var(--muted-foreground))] focus:outline-none focus:ring-2 focus:ring-signal focus:border-transparent"
              />
            </div>

            {/* Password requirements (the same rules the server enforces) */}
            <div id="password-rules" className="grid grid-cols-1 gap-2 py-2 sm:grid-cols-2">
              <PasswordCheck valid={rules.length} text="At least 8 characters" />
              <PasswordCheck valid={rules.upper && rules.lower} text="Upper and lower case" />
              <PasswordCheck valid={rules.number} text="A number" />
              <PasswordCheck valid={rules.special} text="A symbol (!@#$…)" />
              <PasswordCheck valid={passwordMatch} text="Passwords match" />
            </div>

            {siteKey && (
              <TurnstileWidget siteKey={siteKey} onToken={onTurnstileToken} resetKey={turnstileReset} dark={dark} />
            )}

            {error && (
              <div className="p-3 rounded-lg bg-red-900/20 border border-red-800" role="alert">
                <p className="text-sm text-red-400">{error}</p>
              </div>
            )}

            <button
              type="submit"
              disabled={loading || configLoading || !isPasswordValid || needsHumanCheck}
              className="w-full py-3 px-4 rounded-lg bg-accent hover:bg-accent-hover disabled:bg-accent/50 disabled:cursor-not-allowed text-white font-medium transition-colors flex items-center justify-center gap-2"
            >
              {loading ? (
                <>
                  <Loader2 className="w-5 h-5 animate-spin" />
                  Creating account...
                </>
              ) : (
                <>
                  <UserPlus className="w-5 h-5" />
                  Create account
                </>
              )}
            </button>
            {siteKey && needsHumanCheck && isPasswordValid && (
              <p className="text-center text-xs text-[hsl(var(--muted-foreground))]">Complete the human check to continue.</p>
            )}
          </div>
        </form>

        <p className="text-center text-sm text-[hsl(var(--muted-foreground))] mt-6">
          Already have an account?{' '}
          <button
            onClick={onSwitchToLogin}
            className="text-signal-ink hover:text-signal-ink font-medium"
          >
            Sign in
          </button>
        </p>
      </div>
    </div>
  );
}
