# Sign in with GitHub and Google

The dashboard offers **Continue with Google** and **Continue with GitHub** on the
Sign in and Sign up pages. A provider appears only when the API has its
credentials; nothing changes for providers you do not configure (their routes
answer 404 and the button is hidden).

This page is the owner setup checklist, followed by how the flow works and what
it refuses.

## What you need

| Value | Production value |
|-------|------------------|
| API origin (`REMEMBRA_PUBLIC_URL`) | `https://api.remembra.dev` |
| Dashboard origin (`REMEMBRA_PUBLIC_DASHBOARD_URL`) | `https://app.remembra.dev` |
| GitHub callback URL | `https://api.remembra.dev/api/v1/auth/oauth/github/callback` |
| Google redirect URI | `https://api.remembra.dev/api/v1/auth/oauth/google/callback` |

Both origins are exact (scheme + host, no path, no trailing slash). HTTPS is
required; plain `http://` is accepted only for `localhost` / `127.0.0.1`.

The two origins must be **same-site**: subdomains of one registrable domain,
like `api.remembra.dev` and `app.remembra.dev`. The login code the dashboard
receives only works together with an `HttpOnly` cookie the API sets on its own
origin, and browsers send that cookie on the dashboard's request to the API
only when the two are same-site. The API origin must also be in
`REMEMBRA_CORS_ORIGINS` for the dashboard origin (credentials are allowed only
for listed origins; never `*`).

## 1. Create the GitHub OAuth app

1. Open **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**
   (for an organization: **Organization settings → Developer settings → OAuth
   Apps**). Use an *OAuth App*, not a *GitHub App*.
2. Fill in:
    - **Application name:** `Remembra`
    - **Homepage URL:** `https://remembra.dev`
    - **Authorization callback URL:** `https://api.remembra.dev/api/v1/auth/oauth/github/callback`
    - Leave **Enable Device Flow** unchecked.
3. Click **Register application**.
4. Copy the **Client ID**.
5. Click **Generate a new client secret** and copy it now (GitHub shows it once).

The app requests only the `user:email` scope: GitHub then lists the user's
addresses, including private ones, so sign-in works for users who hide their
email.

## 2. Create the Google OAuth client

In the [Google Cloud console](https://console.cloud.google.com/):

1. Create (or select) a project, e.g. `remembra-prod`.
2. Open **Google Auth Platform** (APIs & Services → OAuth consent screen) and
   click **Get started** if asked.
3. **Branding:**
    - App name: `Remembra`
    - User support email: your support address
    - App home page: `https://remembra.dev`
    - Privacy policy: `https://remembra.dev/privacy`
    - Terms of service: `https://remembra.dev/terms`
    - Authorized domains: add `remembra.dev`
    - Developer contact email: your address
    - Uploading an app logo triggers Google's brand verification (takes days).
      Skip the logo, or submit it and wait for approval.
4. **Audience:** User type **External**. Click **Publish app** so the status
   is **In production** (in *Testing*, only listed test users can sign in).
5. **Data access:** add the scopes `openid`, `.../auth/userinfo.email` and
   `.../auth/userinfo.profile`. These are non-sensitive, so no scope
   verification is needed.
6. **Clients → Create client:**
    - Application type: **Web application**
    - Name: `Remembra dashboard`
    - **Authorized JavaScript origins:** leave empty (the API is the client).
    - **Authorized redirect URIs:** `https://api.remembra.dev/api/v1/auth/oauth/google/callback`
      (exact: scheme, case and trailing slash must match).
7. Click **Create** and copy the **Client ID** and **Client secret**.

## 3. Cloudflare Turnstile on Sign up (optional, recommended)

The API already verifies Turnstile when `REMEMBRA_TURNSTILE_SECRET` is set. To
show the widget on the dashboard's Sign up page:

1. **Cloudflare dashboard → Turnstile → Add widget.**
2. Widget name `Remembra signup`, hostname `app.remembra.dev`, mode **Managed**.
3. Copy the **Site key** and the **Secret key**.

Set **both** variables below. With only the secret, dashboard signups fail
("Human verification is required"); with only the site key, the key is not
published and no widget is shown.

## 4. Set the environment in Coolify

In Coolify, open the **Remembra API** application → **Environment Variables**,
add these, then **Redeploy** the API:

```bash
REMEMBRA_PUBLIC_URL=https://api.remembra.dev
REMEMBRA_PUBLIC_DASHBOARD_URL=https://app.remembra.dev
REMEMBRA_GITHUB_CLIENT_ID=<GitHub client ID>
REMEMBRA_GITHUB_CLIENT_SECRET=<GitHub client secret>
REMEMBRA_GOOGLE_CLIENT_ID=<Google client ID>.apps.googleusercontent.com
REMEMBRA_GOOGLE_CLIENT_SECRET=<Google client secret>
# Turnstile (optional; set both or neither)
REMEMBRA_TURNSTILE_SITE_KEY=<Turnstile site key>
REMEMBRA_TURNSTILE_SECRET=<Turnstile secret key>
```

Mark the two client secrets and the Turnstile secret as secrets in Coolify.
`REMEMBRA_PUBLIC_URL` may already be set for the Claude / ChatGPT connector;
keep one value. The dashboard needs no new variables; redeploy it so the new
Sign in / Sign up pages ship.

## 5. Check it

```bash
curl -s https://api.remembra.dev/api/v1/auth/providers
# {"providers":[{"id":"github",...},{"id":"google",...}],"turnstile_site_key":"0x4AAA..."}
```

Then open `https://app.remembra.dev`, click **Continue with GitHub**, approve,
and you land in the dashboard. Repeat with Google.

## How it works

1. The button navigates to `GET /api/v1/auth/oauth/{provider}/start`. The API
   creates a single-use state (10 minutes) with a PKCE `S256` challenge and, for
   Google, an OpenID `nonce`, stores only hashes, binds it to the browser with
   an `HttpOnly`, `SameSite=Lax` cookie (`__Host-` prefixed over HTTPS), and
   redirects to the provider.
2. The provider redirects to `/api/v1/auth/oauth/{provider}/callback`. The API
   checks the state and the cookie, exchanges the code with the PKCE verifier
   and reads a **verified** email:
    - **GitHub:** `GET /user` for the numeric account id and `GET /user/emails`
      for the **primary** address, which must be `verified`. `users.noreply.github.com`
      addresses are refused.
    - **Google:** the `id_token` signature is checked against Google's JWKS
      (RS256), plus `iss`, `aud`, `exp`/`iat` and the `nonce`. `email_verified`
      must be true, and the address must be one Google is authoritative for:
      `@gmail.com`, or a Google Workspace account (`hd` claim).
3. The API picks exactly one account:
    - the account already linked to this provider account signs in;
    - else, an account with the same email whose email was **never verified**
      is **linked** (Google, or GitHub with a verified primary email: nobody
      had proven that mailbox before). The link verifies the email and opens
      a one-time account check (see "Accounts made before email verification"
      below); nothing is revoked and API keys and app connections keep working;
    - else, **Google only**: an account with the same, already verified email
      is **linked**. The account owner gets an email saying Google sign-in was
      added (for every link above);
    - else **GitHub** is refused when an account with that verified email exists
      ("add GitHub in Settings"). GitHub never re-verifies addresses, so a
      "verified" primary email can belong to someone who no longer owns the
      mailbox (a former employer's address, a lapsed domain). GitHub is added
      to an existing account only from a signed-in session (below);
    - else a new account is created with the email marked verified, under the
      same per-network and per-domain signup limits as password signup, unless
      another account (including an API signup) already verified that address.
4. It redirects to `https://app.remembra.dev/oauth/callback#code=...` with a
   single-use login code (5 minutes), and sets a second `HttpOnly`,
   `SameSite=Lax` cookie (`__Host-remembra_oauth_login`) holding a secret bound
   to that code. The dashboard trades the code at
   `POST /api/v1/auth/oauth/exchange` with `credentials: 'include'`; the API
   refuses and burns a code sent without the matching cookie. A code minted in
   someone else's browser (an attacker sending a victim a
   `/oauth/callback#code=...` link to sign them into the attacker's account)
   therefore cannot be used. Accounts with 2FA must still enter their TOTP code
   there, from the same browser.

Accounts created this way have no password. **Forgot password** sets one.

### Connecting a provider to an existing account

**Settings → Security → Sign-in methods** lists Google and GitHub with
**Connect** / **Disconnect**. Connect calls
`POST /api/v1/auth/oauth/{provider}/link` with the dashboard session, which
returns a single-use start path (2 minutes) that the browser opens. The same
call (made with `credentials: 'include'`) sets an HttpOnly cookie that binds the
start path to that browser: opened anywhere else (for example a link someone
minted for their own account and sent to you), it is refused and burned before
the provider is reached. The rest is the normal flow, and the callback attaches the provider account to the signed-in
account (the provider email may differ from the account email) and sends the
browser back to Settings. It needs a session from the last 15 minutes; with an
older one the page asks the user to sign in again. A provider account already
connected to another Remembra account is refused. The owner is emailed whenever
a provider is added. `GET /api/v1/auth/identities` lists connections and
`DELETE /api/v1/auth/identities/{provider}` removes one.

## What is refused, and what users see

| Case | Result |
|------|--------|
| GitHub primary email not verified, or a noreply address | "needs a verified primary email address" |
| Google `email_verified` false | "did not confirm your email address" |
| Google address that is neither Gmail nor Workspace | "Google cannot confirm who owns this email address" (sign up with email instead) |
| Google or GitHub (verified email), and an account with that email exists but was never verified | Signed in and linked; the email becomes verified and the account check opens (below) |
| Same case, but an API signup already verified that address | Refused (one free account per verified email) |
| GitHub, and an account with that (verified) email already exists | Refused: "Sign in with Google or your password first. Then add GitHub in Settings." |
| No account has the email, but an API signup already verified it | Refused (one free account per verified email) |
| This Remembra account is already linked to a different GitHub / Google account | Refused |
| Connecting a provider account that is already connected to another Remembra account | Refused |
| State missing, expired, replayed, from another browser, or nonce mismatch | Refused ("did not start in this browser") |
| Deactivated account | Refused |
| Too many new accounts from one network | Refused (signup limits) |

One account per verified email and per provider account: `user_identities` is
unique on `(provider, provider_user_id)` and on `(user_id, provider)`, and
`users.email` is unique.

### Accounts made before email verification

Accounts made before email verification existed, and any account whose owner
never clicked the link, have an unverified email. Anyone could have signed up
with another person's address and a password and set things up on it. So the
first proof that someone owns the mailbox (**Sign in with Google** or
**GitHub** with a verified email, or an emailed **Forgot password** reset)
does not wipe the account. It:

1. marks the email verified (unless another account already verified it);
2. signs out every dashboard session opened before (API keys and app
   connections such as Claude or ChatGPT keep working: no agent is
   disconnected), and cancels any "connect a sign-in method" flow in progress;
3. opens a one-time **account check** (`account_reviews`, schema migration 10);
4. shows the owner, right after sign-in, everything on the account: API keys
   (name, created, last used, access, projects, agent), app connections,
   webhooks, every other sign-in link, 2FA turned on before verification, and
   the password when it was set before verification. **Keep all** finishes in
   one click and keeps exactly the list on screen (if anything changed
   meanwhile, the new list is shown instead). Each item can be revoked on its
   own, and the password removed. 2FA from before verification is kept only
   by entering a current code from the authenticator; otherwise it is turned
   off when the check finishes, so an authenticator the owner never had
   cannot lock them out. "Later" hides the check for the browser session; it
   comes back until it is done.

A check with nothing to list (say a reset on an account with no keys, apps,
webhooks or 2FA) finishes by itself: no screen and no email.

Only a session that proved the mailbox can act on the check: a sign-in with
the exact Google or GitHub account that opened it (or one connected from such
a session, or a Google sign-in that matches the address later), or a password
set through the emailed reset. Those sessions carry the check's id as the
`rvw` JWT claim. Any other session (a password or a sign-in link set up before
verification) still signs in, but sees no check, cannot turn on 2FA, connect
or disconnect a sign-in method, create API keys or webhooks, or connect a new
app until it is done, and an owner address gets no superadmin rights or owner
plan until then. 2FA set up before verification does not apply to the proven
sessions (it is one of the items to check). Removing the password or a
sign-in link signs out every other dashboard session; app connections are
revoked only one by one.

Every revoke, keep, "later", finish, trusted sign-in and change of trust is
written to the audit log (`account_review_*`; connecting and disconnecting
sign-in methods always writes `identity_linked` / `identity_unlinked`), and the
finished check is emailed to the account address.

API: `GET /api/v1/auth/review` (includes `version`),
`POST /api/v1/auth/review/revoke`
(`{"kind": "key|connection|webhook|identity|two_factor|password", "id": ...}`),
`POST /api/v1/auth/review/keep` (`{"kind": "two_factor", "code": "123456"}`),
`POST /api/v1/auth/review/complete` (`{"version": ...}`, 409 when the list
changed), `POST /api/v1/auth/review/defer` (dashboard session only).

GitHub is linked by email only into an account nobody has verified yet. Into
a verified account it is connected in Settings after signing in.

Rate limits per client IP: `start` 20/minute, `callback` 30/minute, `exchange`
10/minute, `providers` 60/minute, `link` 10/minute.

## Logs

Provider tokens, codes, states, verifiers and nonces are never logged. The API
redacts `code`/`state` query values from its own access log. Reverse proxies in
front of it (Coolify's Traefik, Cloudflare) log full URLs by default; if they
keep access logs, have them drop query strings for
`/api/v1/auth/oauth/*/callback`. The codes are single use and PKCE-bound, so a
logged code cannot be replayed, but there is no reason to keep them.

## Email verification for API signups

Tenants created by `POST /api/v1/cloud/signup` (the master-key signup backend)
have no dashboard login. They get a verification link at signup
(`email_verification_sent` in the response) and can ask for a new one with their
API key:

```bash
curl -X POST https://api.remembra.dev/api/v1/cloud/verify-email/request \
  -H "X-API-Key: rem_..."
```

The link opens `https://app.remembra.dev/verify-email?token=...&account=api`,
which confirms it with `POST /api/v1/cloud/verify-email/confirm` (token only,
single use, 24 hours). An address already verified on another account cannot be
verified again, so one email backs one free account. The rule holds in every
direction: the dashboard's verify-email confirm answers `409`, Sign in with
Google / GitHub will not create a second account, and a password reset does not
mark the address verified, when another account (dashboard or API signup)
already verified it. Once
`REMEMBRA_UNVERIFIED_CREDIT_CAP_EFFECTIVE_AT` is set, these tenants are held at
25 credits until they verify, like dashboard signups.

Dashboard password signups receive their link automatically (when email is
configured) and confirm it while signed in; the `/verify-email` page asks them
to sign in first if needed. **Settings → Profile** shows whether the email is
verified and has **Resend verification email** (`POST /api/v1/auth/verify-email/request`)
for links that expired.
