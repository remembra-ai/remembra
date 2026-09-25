# Claude & ChatGPT apps (pick up from anywhere)

Connect Remembra to the **Claude apps** (claude.ai, Claude Desktop, and the Claude
iPhone/Android apps) and to **ChatGPT**, so you can check on your agents' work from
your phone and leave them instructions.

From a chat on your phone you can:

| Ask for | Tool | What it does |
|---|---|---|
| "Where did we leave the billing work?" | `session_brief` | Latest handoff, current status values, recent work by time, and an agent's unread inbox |
| "What have my agents done this week?" | `trail` | Handoffs and checkpoints your agents left, newest first |
| "What did we decide about the invoice format?" | `recall_memories` | Searches your memory (semantic + keyword) |
| "Tell Claude Code to fix the flaky login test" | `send_to_inbox` | Leaves an instruction in an agent's inbox |
| "Note: call the printer vendor Monday" | `store_memory` | Saves a note (always a new memory) |
| "Which projects can you see?" | `list_projects` | The projects this connection may use |

Nothing over this connector edits or deletes memories.

**How the phone-to-desktop hand-off works.** `send_to_inbox` with `to_agent: "claude-code"`
puts a message in Claude Code's inbox. The next time Claude Code starts a session and calls
`session_brief` (the Remembra session-start hook does this for you), the message is at the top
of its brief, from the agent name you gave this connection (for example `claude-app`). The
desktop agent must run with `REMEMBRA_AGENT_ID=claude-code` for its inbox to match.

**Turn the connector off inside desktop Claude Code.** A connector you add on claude.ai is
also loaded automatically by Claude Code when you sign in with the same Claude account. It
shows up in `/mcp` as `claude.ai Remembra`, next to Claude Code's own Remembra server (the
API-key setup that runs as `claude-code`). Both offer `session_brief`, `recall_memories`,
`store_memory` and `send_to_inbox`, and if Claude Code picks the connector's copy it acts as
`claude-app`: its brief shows `claude-app`'s inbox (so it never sees the phone's
instruction), its notes and replies are signed `claude-app`, and it can only use the
connection's projects. Keep the connector for the phone and turn it off in Claude Code with
any one of these:

- In Claude Code, run `/mcp`, select `claude.ai Remembra` and disable it (this project only).
- Block just this connector in your Claude Code settings (`~/.claude/settings.json`):

    ```json
    { "deniedMcpServers": [{ "serverName": "claude.ai Remembra" }] }
    ```

    Use the exact name `/mcp` shows if you named the connector differently.

- Turn off all claude.ai connectors in Claude Code: `"disableClaudeAiConnectors": true` in
  settings, or start it with `ENABLE_CLAUDEAI_MCP_SERVERS=false claude`.

Sources: [Claude Code: MCP](https://code.claude.com/docs/en/mcp) ("Use MCP servers from
Claude.ai"), [Claude: get started with connectors](https://claude.com/docs/connectors/getting-started).

## Where this works

Checked against the vendors' documentation on 2026-09-25:

| App | Plans | Where you add it | Works on phone |
|---|---|---|---|
| Claude (custom connector) | Free (1 custom connector), Pro, Max, Team, Enterprise | claude.ai or Claude Desktop: **Customize > Connectors** | Yes. Anthropic lists "Claude mobile app: remote connectors"; a connector added on the web is available in your chats on web, desktop and mobile |
| Claude Team / Enterprise | An Owner adds the connector for the organization, then each member connects their own Remembra account | **Organization settings > Connectors** | Yes |
| Claude Code | Any | `claude mcp add` (see below) | n/a |
| ChatGPT (developer mode) | Plus, Pro, Business, Enterprise, Education | ChatGPT **on the web**, Developer mode | Not confirmed. OpenAI's developer-mode guide says "on the web"; its help-center article could not be read to confirm mobile. Plan on web only. |

Sources: [Claude: add a connector that isn't in the directory](https://claude.com/docs/connectors/custom/add-unlisted),
[Claude: get started with connectors](https://claude.com/docs/connectors/getting-started),
[Claude: authentication for connectors](https://claude.com/docs/connectors/building/authentication),
[Claude Help Center: custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp),
[OpenAI: ChatGPT developer mode](https://developers.openai.com/api/docs/guides/developer-mode),
[OpenAI: Apps SDK authentication](https://developers.openai.com/apps-sdk/build/auth).

## Connect Claude (web, desktop and phone)

You need your Remembra account email and password (the ones you use for the dashboard),
plus your 2FA code if you turned 2FA on.

1. On claude.ai or in Claude Desktop, open **Customize > Connectors**.
2. Click **Add custom connector** (on Team/Enterprise an Owner does this under
   **Organization settings > Connectors > Add > Custom**).
3. Enter the server URL: `https://api.remembra.dev/mcp`
   (self-hosted: `<your REMEMBRA_PUBLIC_URL>/mcp`). Leave the OAuth client ID and secret empty;
   Claude registers itself automatically.
4. Click **Add**, then **Connect**.
5. A Remembra sign-in page opens. Sign in with your Remembra email and password (and 2FA code).
6. On the next page choose:
    - **Projects it may use**: tick one or more. The first one you tick is the default.
      You can also type a new project name.
    - **Agent name for this connection**: defaults to `claude-app`. Your other agents see
      notes and inbox messages from this connection under this name.
7. Click **Allow**. The connector shows **Connected**.
8. On your phone, open the Claude app, start a chat, tap **+ > Connectors** and turn
   **Remembra** on for the chat.

Try: *"Using Remembra, what's the latest handoff for my project?"* or
*"Tell claude-code to rerun the migration tests tomorrow morning."*

## Connect ChatGPT (web)

Following [OpenAI's developer-mode guide](https://developers.openai.com/api/docs/guides/developer-mode)
(Pro, Plus, Business, Enterprise and Education accounts, on the web):

1. In ChatGPT on the web, open **Settings > Security and login** and turn on
   **Developer mode**.
2. Open **ChatGPT Plugins** and select the **+** button (it only works once Developer mode is
   on) to create a developer-mode app for a remote MCP server:
    - **Name**: *Remembra*
    - **MCP server URL**: `https://api.remembra.dev/mcp`
    - **Authentication**: **OAuth**, using **dynamic client registration (DCR)**. Leave the
      OAuth **client ID and client secret empty** and do not pick Client ID Metadata
      Document (CIMD): Remembra supports only DCR. If you fill in a client ID/secret,
      ChatGPT uses those static values and the sign-in fails with "Unknown client".
3. ChatGPT opens the Remembra sign-in page. Sign in, choose projects and an agent name
   (defaults to `chatgpt`), and click **Allow**.
4. In a chat, enable the Remembra app. ChatGPT asks you to confirm each write
   (`send_to_inbox`, `store_memory`) before it runs.

## Connect Claude Code (optional)

Claude Code can use the same connector instead of an API key:

```bash
claude mcp add --transport http remembra-connector https://api.remembra.dev/mcp
```

Then run `/mcp` in Claude Code and choose the server to sign in. The sign-in page warns that
the result goes to `localhost`; that is expected for Claude Code. Use a distinct agent name
(for example `claude-code-connector`) if Claude Code already uses `claude-code` through its
API-key setup, so inbox messages don't split between the two.

This is separate from the claude.ai connector, which Claude Code also loads on its own when
you're signed in with your Claude account; see "Turn the connector off inside desktop Claude
Code" above.

## Manage or disconnect

- **From Claude or ChatGPT**: disconnect or remove the connector in the app's connector
  settings.
- **From Remembra**: `GET /api/v1/connector/connections` lists every connected app (with the
  dashboard login token as `Authorization: Bearer <jwt>`), and
  `DELETE /api/v1/connector/connections/<connection_id>` disconnects one immediately.
- Changing or resetting your password, or deactivating the account, disconnects all apps,
  including a sign-in that was still on the consent page: approving it after the password
  change is refused.

## Security model

- **OAuth 2.1 authorization code flow with PKCE (S256 only)**, dynamic client registration
  (RFC 7591), authorization-server metadata (RFC 8414) and protected-resource metadata
  (RFC 9728). Authorization responses include `iss` (RFC 9207).
- **Only known callbacks can register**: `https://claude.ai/api/mcp/auth_callback`,
  `https://chatgpt.com/connector_platform_oauth_redirect`,
  `https://chatgpt.com/connector/oauth/<id>`, and `http://localhost` / `127.0.0.1` / `[::1]` on
  any port for local clients. Anything else is refused, so a registered client can never send
  your sign-in to another site.
- **Tokens are bound to you, the chosen projects and this server** (`resource`
  `https://api.remembra.dev/mcp`). Access tokens last 1 hour; refresh tokens 30 days and are
  replaced on every use. A refresh sent again within 30 seconds (a client retry) gets back the
  same new pair, never a second one, so each connection has exactly one live refresh token.
  A refresh token that comes back later than that, or after its replacement was itself used,
  or an authorization code used twice, ends the whole connection.
- **Every token, code and client secret is stored only as a SHA-256 hash.**
- **Scopes**: `session:brief` (brief and trail), `memory:recall` (search),
  `memory:store` (notes and inbox messages only). A call without the needed scope gets
  `403 insufficient_scope`. Scopes Remembra doesn't know (for example a client's own
  `claudeai`) are ignored at registration and dropped at sign-in; the consent page and the
  token response list exactly what was granted.
- **Projects cover the inbox too.** `session_brief` over the connector only shows inbox
  messages tagged with one of the connection's projects, and only agent names seen in those
  messages. Messages the connector sends are tagged with their project, and so are messages
  sent from the desktop MCP server (with its `REMEMBRA_PROJECT`); untagged messages from
  other clients stay out of the connector's view.
- **The connector token only works at `/mcp`.** Tools reach your memory through the same
  REST routes and checks every other client goes through (project restriction, PII policy,
  content sanitizer, usage limits, audit log). Audit entries carry `oauth:<connection_id>`.
- Sign-in shares the dashboard's per-account lockout (5 failed attempts locks the account for
  15 minutes) and is rate limited per IP.

## For the server owner: production setup

The connector is **off by default**. To turn it on (do not skip any step):

1. **Environment variables** on the API service:

    | Variable | Value |
    |---|---|
    | `REMEMBRA_CONNECTOR_ENABLED` | `true` |
    | `REMEMBRA_PUBLIC_URL` | `https://api.remembra.dev` (origin only, no path; must be HTTPS) |
    | `REMEMBRA_AUTH_ENABLED` | `true` (already set in production; the connector refuses to start without it) |
    | `REMEMBRA_JWT_SECRET` | already set; the sign-in page uses the dashboard accounts |
    | `REMEMBRA_CONNECTOR_ACCESS_TOKEN_TTL_SECONDS` | optional, default `3600` |
    | `REMEMBRA_CONNECTOR_REFRESH_TOKEN_TTL_DAYS` | optional, default `30` |
    | `REMEMBRA_CONNECTOR_REDIRECT_URIS` | optional JSON list of extra exact callback URLs for other MCP clients |
    | `REMEMBRA_CONNECTOR_ALLOW_LOOPBACK_REDIRECTS` | optional, default `true` (Claude Code); `false` to allow only hosted apps |

2. **Rebuild the image.** `Dockerfile.cloud` now installs the `mcp` extra (the MCP SDK at the
   version pinned in `uv.lock`); the API fails at startup with a clear message if the connector
   is enabled without it.
3. **Cloudflare / WAF.** Claude connects from `160.79.104.0/21`
   ([Anthropic IP ranges](https://platform.claude.com/docs/en/api/ip-addresses)); ChatGPT from
   OpenAI's ranges. Make sure no bot-fight, challenge or WAF rule blocks or challenges
   `/mcp`, `/oauth/*` and `/.well-known/oauth-*`, and that these paths are not cached.
   Claude's docs call out a WAF in front of the authorization server as a common cause of
   failed connections.
4. **Verify after deploy:**

    ```bash
    curl -s https://api.remembra.dev/.well-known/oauth-authorization-server | python3 -m json.tool
    curl -s https://api.remembra.dev/.well-known/oauth-protected-resource/mcp | python3 -m json.tool
    curl -si -X POST https://api.remembra.dev/mcp -H 'Content-Type: application/json' -d '{}' | grep -i www-authenticate
    # expect: Bearer resource_metadata="https://api.remembra.dev/.well-known/oauth-protected-resource/mcp", scope="..."
    ```

    Then add the connector in claude.ai as above and call `session_brief` from the phone app.

### Known limits

- Clients register with **dynamic client registration**. Claude documentation recommends a
  Client ID Metadata Document (CIMD) for servers expecting heavy directory traffic because DCR
  creates one client per connection; Remembra prunes clients idle for 30 days without a live
  connection. CIMD is not implemented (it requires fetching client-supplied URLs, an SSRF
  surface that needs its own review).
- `/oauth/register` (60/min) and `/oauth/token` (120/min) are rate limited per source IP, and
  Claude's and ChatGPT's servers each call from a shared range. Raise these if many users
  connect at once.
- There is no dashboard screen for connections yet; use the API above.
