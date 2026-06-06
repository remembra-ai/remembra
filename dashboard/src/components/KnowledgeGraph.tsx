import { useState } from 'react';
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

interface KnowledgeGraphProps {
  projectId?: string;
}

/**
 * Knowledge graph with two views:
 *  - "Universe": the 3D neural-universe render (glowing nodes, firing synapses,
 *    bloom, slow orbital drift). Default when WebGL is available.
 *  - "Flat": the dependable 2D force graph (fallback / preference).
 *
 * Both share the same data + the click-to-see-real-memories panel.
 */
export function KnowledgeGraph({ projectId }: KnowledgeGraphProps) {
  const [mode, setMode] = useState<'universe' | 'flat'>(() => (webglAvailable() ? 'universe' : 'flat'));

  return (
    <div className="relative">
      <div className="absolute top-3 left-1/2 -translate-x-1/2 z-30 flex items-center gap-1 rounded-full bg-black/55 backdrop-blur-md border border-white/10 p-1 shadow-lg">
        <button
          onClick={() => setMode('universe')}
          className={
            'px-3.5 py-1.5 rounded-full text-xs font-semibold transition-colors ' +
            (mode === 'universe' ? 'bg-purple-500/80 text-white shadow-[0_0_12px_rgba(168,85,247,0.5)]' : 'text-gray-300 hover:text-white')
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

      {mode === 'universe' ? <EntityGraphUniverse projectId={projectId} /> : <EntityGraph projectId={projectId} />}
    </div>
  );
}
