import { createContext, useContext } from 'react';
import type { CrewRuntime } from './runtime';

/** The signed-in tab's crew runtime (see CrewProvider). */
export const CrewContext = createContext<CrewRuntime | null>(null);

export function useCrewRuntime(): CrewRuntime {
  const runtime = useContext(CrewContext);
  if (!runtime) throw new Error('useCrewRuntime must be used inside <CrewProvider>');
  return runtime;
}
