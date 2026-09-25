# Remembra dashboard

The back office users see after signing in: mission control for Remembra Relay
(the handoff trail, agent activity, the agent inbox) plus memory, graph and
account pages. React 19, Vite 7, Tailwind 4, TypeScript (strict).

## Pages

| Route | What it shows | APIs |
|-------|---------------|------|
| `#/home` | Greeting, what changed since the last visit, the last handoff with a copyable "continue with" command, unread messages, the week's recap, plan usage, the connect checklist for new users | `GET /trail`, `/trail/summary`, `/inbox/summary`, `/inbox/messages`, `/cloud/usage` |
| `#/trail?project=&agent=&open=` | Every handoff and checkpoint, newest first, grouped by day; each node expands to its Done / Not done / Failing / Next | `GET /trail` |
| `#/agents` | One card per agent: last active, sessions this week, 14-day sparkline, active-now pulse | `GET /trail/summary` |
| `#/inbox?status=&agent=&compose=1&to=` | Agent-to-agent messages; mark read / done; write to an agent (it leads that agent's next session brief) | `GET /inbox/messages`, `POST /inbox/send`, `POST /inbox/{id}/ack` |
| `#/memories` … `#/admin` | Memory, graph and settings pages | existing endpoints |

Relay data is polled every 30 seconds while the tab is visible. Polling stops on
failures a retry cannot fix (401, 403, 404, 503) until the user retries.

## Design tokens

`src/index.css` holds the "baton trail" tokens (stone paper, graphite ink,
signal orange reserved for the baton) for light and dark themes, exposed to
Tailwind as `paper`, `panel`, `ink`, `ink-2`, `ink-3`, `rule`, `signal`,
`signal-ink`, `accent`, `ok`, `fail`. Older pages use the `hsl(var(--x))`
tokens and the stock gray / purple scales, which are mapped onto the same
palette. Fonts: Bricolage Grotesque (display), Hanken Grotesk (body),
JetBrains Mono (metadata).

## Develop

```bash
npm ci
REMEMBRA_API_PROXY=http://localhost:8787 npm run dev   # proxies /api, /ws and /health
npm test          # unit tests (vitest)
npm run lint
npm run build     # tsc -b && vite build -> dist/
```

The production image (`Dockerfile.cloud`, `dashboard-builder` stage) runs
`npm ci` and `npm run build` and serves `dist/`.

Keyboard: `⌘K` or `/` search and commands, `c` write to an agent, `?`
shortcuts, `g` then `h` / `t` / `a` / `i` / `m` / `g` / `s` to jump.
