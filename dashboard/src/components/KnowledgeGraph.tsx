import { Component, useState, type ReactNode } from 'react';
import { EntityGraph } from './EntityGraph';
import { EntityGraphUniverse } from './EntityGraphUniverse';

function webglAvailable(): boolean {
  try {
    const canvas = document.createElement('canvas');
    return !!(
      window.WebGLRenderingContext &&
      (canvas.getContext('webgl') || canvas.getContext('experimental-webgl'))
    );
  } catch {
    return false;
  }
}

/**
 * Catches any render-time failure in the 3D view and shows a fallback instead of
 * letting it bubble to the app-level boundary (which would blank the whole panel).
 */
class GraphErrorBoundary extends Component<{ fallback: ReactNode; children: ReactNode }, { hasError: boolean }> {
  state = { hasError: false };
  static getDerivedStateFromError() {
    return { hasError: true };
  }
  componentDidCatch(error: unknown) {
    // eslint-disable-next-line no-console
    console.error('[KnowledgeGraph] 3D view failed; falling back to 2D:', error);
  }
  render() {
    return this.state.hasError ? this.props.fallback : this.props.children;
  }
}

interface KnowledgeGraphProps {
  projectId?: string;
}

/**
 * Knowledge graph with two views:
 *  - "Universe": the 3D neural-universe render (glowing nodes, firing synapses,
 *    bloom, slow orbital drift).
 *  - "Flat": the dependable 2D force graph.
 *
 * Defaults to Flat for safety; the 3D view is opt-in via the toggle and wrapped in
 * an error boundary that falls back to Flat so the panel can never be taken down by
 * the graph. Both views share the click-to-see-real-memories panel.
 */
export function KnowledgeGraph({ projectId }: KnowledgeGraphProps) {
  const [mode, setMode] = useState<'universe' | 'flat'>('flat');
  const canUniverse = webglAvailable();

  return (
    <div className="relative">
      <div className="absolute top-3 left-1/2 -translate-x-1/2 z-30 flex items-center gap-1 rounded-full bg-black/55 backdrop-blur-md border border-white/10 p-1 shadow-lg">
        <button
          onClick={() => canUniverse && setMode('universe')}
          disabled={!canUniverse}
          title={canUniverse ? 'Immersive 3D view' : 'WebGL not available'}
          className={
            'px-3.5 py-1.5 rounded-full text-xs font-semibold transition-colors ' +
            (mode === 'universe'
              ? 'bg-purple-500/80 text-white shadow-[0_0_12px_rgba(168,85,247,0.5)]'
              : canUniverse
                ? 'text-gray-300 hover:text-white'
                : 'text-gray-600 cursor-not-allowed')
          }
        >
          ✨ Universe
        </button>
        <button
          onClick={() => setMode('flat')}
          className={
            'px-3.5 py-1.5 rounded-full text-xs font-semibold transition-colors ' +
            (mode === 'flat' ? 'bg-white/20 text-white' : 'text-gray-300 hover:text-white')
          }
        >
          Flat
        </button>
      </div>

      {mode === 'universe' ? (
        <GraphErrorBoundary key="universe" fallback={<EntityGraph projectId={projectId} />}>
          <EntityGraphUniverse projectId={projectId} />
        </GraphErrorBoundary>
      ) : (
        <EntityGraph projectId={projectId} />
      )}
    </div>
  );
}
