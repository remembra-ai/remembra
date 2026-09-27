// A server with Crew mode off (REMEMBRA_CREW_MODE unset: GET /crews is a 404) gets the dashboard it had
// before crews: no Crews in the navigation, and an old crew link says crews are off instead of "update
// Remembra on the server".

import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Sidebar } from '../../../components/Sidebar';
import { createCrewApi } from '../api';
import { CrewContext } from '../context';
import { CrewRoutes } from '../CrewRoutes';
import { CrewRuntime } from '../runtime';
import { ManualTimers, SocketFactory, flush } from './fakes';

async function runtimeAnswering(status: number): Promise<CrewRuntime> {
  const api = createCrewApi({
    baseUrl: '',
    credentials: () => ({ jwt: 't' }),
    fetch: async () =>
      new Response(JSON.stringify(status === 200 ? { crews: [], count: 0 } : { detail: 'Not Found' }), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
  });
  const runtime = new CrewRuntime({
    api,
    socketUrl: 'ws://x/ws',
    credentials: () => ({ jwt: 't' }),
    createSocket: new SocketFactory().create,
    timers: new ManualTimers(),
  });
  runtime.probeCrewMode();
  await flush(20);
  return runtime;
}

const noop = () => {};

function sidebar(runtime: CrewRuntime): string {
  return renderToStaticMarkup(
    <CrewContext.Provider value={runtime}>
      <Sidebar
        activeTab="home"
        isAdmin={false}
        inboxUnread={0}
        darkMode={false}
        onToggleDarkMode={noop}
        onShowShortcuts={noop}
        onOpenConnection={noop}
        onLogout={noop}
      />
    </CrewContext.Provider>,
  );
}

describe('dashboard with Crew mode off on the server', () => {
  it('leaves Crews out of the navigation, and keeps it when the server runs crews', async () => {
    expect(sidebar(await runtimeAnswering(404))).not.toContain('href="#/crews"');
    const on = sidebar(await runtimeAnswering(200));
    expect(on).toContain('href="#/crews"');
    expect(on).toContain('href="#/inbox"');
  });

  it('answers an old crew link with "Crew mode is off", not "update Remembra"', async () => {
    const html = renderToStaticMarkup(
      <CrewContext.Provider value={await runtimeAnswering(404)}>
        <CrewRoutes tab="crews" />
      </CrewContext.Provider>,
    );
    expect(html).toContain('This server runs without Crew mode.');
    expect(html).toContain('REMEMBRA_CREW_MODE=true');
    expect(html).not.toContain('Update Remembra');
  });
});
