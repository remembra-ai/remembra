# Remembra dashboard

The back office users see after signing in: mission control for Remembra Relay
(the handoff trail, agent activity, the agent inbox) plus memory, graph and
account pages. React 19, Vite 7, Tailwind 4, TypeScript (strict).

## Pages

| Route | What it shows | APIs |
|-------|---------------|------|
| `#/home` | Greeting, what changed since the last visit, the last handoff with a copyable "continue with" command, unread messages, the week's recap, smart credits left, the connect checklist (create a relay key, one-line install) for new users | `GET /trail`, `/trail/summary`, `/inbox/summary`, `/inbox/messages`, `/cloud/usage/summary`, `POST /keys` |
| `#/trail?project=&agent=&open=` | Every handoff and checkpoint, newest first, grouped by day; each node expands to its Done / Not done / Failing / Next | `GET /trail` |
| `#/agents` | One card per agent: last active, sessions this week, 14-day sparkline, active-now pulse | `GET /trail/summary` |
| `#/inbox?status=&agent=&compose=1&to=` | Agent-to-agent messages; mark read / done; write to an agent (it leads that agent's next session brief) | `GET /inbox/messages`, `POST /inbox/send`, `POST /inbox/{id}/ack` |
| `#/graph?project=&agent=&window=` | The Constellation: agents, projects, handoffs, checkpoints and entities on a canvas; orange packets travel when a handoff lands or a note is sent; click a node for its drawer | `GET /trail`, `/trail/summary`, `/inbox/messages`, `/debug/entities/graph`, `/entities/{id}/…`, `/ws` |
| `#/billing` | Smart credits for the period (monthly or yearly bank), degraded state, relay/recall/memory usage, plans and Paddle checkout (Team seats >= 3) | `GET /cloud/usage/summary`, `/cloud/context`, `/billing/plans`, `POST /billing/checkout`, `/billing/portal` |
| `#/connections` | Apps connected through the remote connector (Claude, ChatGPT): scopes, projects, last used, revoke | `GET/DELETE /connector/connections` (email sign-in) |
| `#/memories` … `#/admin` | Memory, entities, brain and settings pages | existing endpoints |

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

Dark is the default theme for every visitor; the theme toggle saves an explicit
choice (`darkMode` in local storage) that always wins.

## Brand assets

The mark, lockups, favicons and `src/brand/geometry.ts` are generated, not
drawn by hand in an editor, and they come from the same source as the
marketing site's: `scripts/brand/geometry.py` (repo root) builds the five-lobe
brain as one compound path, the monoline wordmark and the hand-placed 16 and
32 px pixel grids; `scripts/brand/build.py` writes `public/brand/*.svg`,
`public/favicon.svg` (the 16 px pixel tile), `favicon.ico`, the PNG icons and
the TypeScript geometry the React mark, the sign-in pixel hero and the
first-handoff scene draw from, and the identical set for `landing/`. Never
edit these files here; change the geometry and rebuild both.

```bash
python3 scripts/brand/build.py   # from the repo root; needs shapely, Pillow, rsvg-convert, ImageMagick
```

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
