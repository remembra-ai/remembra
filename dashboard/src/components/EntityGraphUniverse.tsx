import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import ForceGraph3D from 'react-force-graph-3d';
import * as THREE from 'three';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { api, type EntityMemoriesResponse } from '../lib/api';
import { Sparkles, RefreshCw } from 'lucide-react';

interface EntityGraphUniverseProps {
  projectId?: string;
}

interface UniNode {
  id: string;
  name: string;
  type: string;
  memoryCount: number;
  color: string;
  val: number;
}

interface UniLink {
  source: string;
  target: string;
  type: string;
}

const TYPE_COLORS: Record<string, string> = {
  person: '#3b82f6',
  organization: '#a855f7',
  company: '#a855f7',
  org: '#a855f7',
  location: '#22c55e',
  place: '#22c55e',
  concept: '#f59e0b',
  money: '#10b981',
  date: '#06b6d4',
  event: '#ec4899',
  product: '#8b5cf6',
  technology: '#14b8a6',
};

function colorForType(type: string): string {
  return TYPE_COLORS[type.toLowerCase()] || '#8b93a7';
}

function nodeRadius(n: UniNode): number {
  // Smaller than before — huge spheres were part of the blinding when zoomed in.
  return 2 + Math.min(n.memoryCount, 30) * 0.32;
}

/** Crisp text label rendered to a canvas texture (no extra dependency). */
function makeLabelSprite(text: string): THREE.Sprite {
  const pad = 8;
  const fontPx = 44;
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  if (!ctx) return new THREE.Sprite();
  const font = `600 ${fontPx}px Inter, system-ui, sans-serif`;
  ctx.font = font;
  const textW = Math.ceil(ctx.measureText(text).width);
  canvas.width = textW + pad * 2;
  canvas.height = fontPx + pad * 2;
  ctx.font = font; // context resets when the canvas is resized
  ctx.textBaseline = 'middle';
  ctx.fillStyle = 'rgba(214,220,250,0.92)';
  ctx.fillText(text, pad, canvas.height / 2);
  const texture = new THREE.CanvasTexture(canvas);
  texture.minFilter = THREE.LinearFilter;
  texture.needsUpdate = true;
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false });
  const sprite = new THREE.Sprite(material);
  const worldHeight = 5;
  sprite.scale.set(worldHeight * (canvas.width / canvas.height), worldHeight, 1);
  return sprite;
}

/** A readable node: crisp solid core + soft additive halo (controlled glow) + label. */
function makeNodeObject(n: UniNode): THREE.Object3D {
  const r = nodeRadius(n);
  const group = new THREE.Group();
  const halo = new THREE.Mesh(
    new THREE.SphereGeometry(r * 2.3, 16, 16),
    new THREE.MeshBasicMaterial({
      color: n.color,
      transparent: true,
      opacity: 0.14,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    }),
  );
  group.add(halo);
  const core = new THREE.Mesh(
    new THREE.SphereGeometry(r, 18, 18),
    new THREE.MeshBasicMaterial({ color: n.color }),
  );
  group.add(core);
  const label = makeLabelSprite(n.name);
  label.position.set(0, r + 4, 0);
  group.add(label);
  return group;
}

export function EntityGraphUniverse({ projectId }: EntityGraphUniverseProps) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fgRef = useRef<any>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const sceneConfigured = useRef(false);

  const [graphData, setGraphData] = useState<{ nodes: UniNode[]; links: UniLink[] }>({ nodes: [], links: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dimensions, setDimensions] = useState({ width: 960, height: 720 });

  const [selectedNode, setSelectedNode] = useState<UniNode | null>(null);
  const [nodeMemories, setNodeMemories] = useState<EntityMemoriesResponse['memories']>([]);
  const [memoriesTotal, setMemoriesTotal] = useState(0);
  const [memoriesLoading, setMemoriesLoading] = useState(false);
  const [memoriesError, setMemoriesError] = useState(false);
  const [memoriesRetry, setMemoriesRetry] = useState(0);

  // ---- Size to container -------------------------------------------------
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const apply = (w: number, h: number) =>
      setDimensions((prev) => {
        const next = { width: Math.max(Math.floor(w), 320), height: Math.max(Math.floor(h), 560) };
        return prev.width === next.width && prev.height === next.height ? prev : next;
      });
    const ro = new ResizeObserver((entries) => {
      const e = entries[0];
      if (e) apply(e.contentRect.width, e.contentRect.height);
    });
    ro.observe(el);
    const rect = el.getBoundingClientRect();
    apply(rect.width, rect.height);
    return () => ro.disconnect();
  }, []);

  // ---- Load graph --------------------------------------------------------
  const fetchGraphData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getEntityGraph(projectId || undefined);
      const nodes: UniNode[] = data.nodes.map((n) => {
        const count = n.memory_count || 1;
        return {
          id: n.id,
          name: n.label,
          type: n.type.toLowerCase(),
          memoryCount: count,
          color: colorForType(n.type),
          val: 1.5 + Math.min(count, 40) * 0.9,
        };
      });
      const ids = new Set(nodes.map((n) => n.id));
      const links: UniLink[] = data.edges
        .filter((e) => ids.has(e.source) && ids.has(e.target))
        .map((e) => ({ source: e.source, target: e.target, type: e.type || 'related' }));
      setGraphData({ nodes, links });
      setSelectedNode(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load graph');
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    fetchGraphData();
  }, [fetchGraphData]);

  // ---- Cinematic scene: bloom + starfield + slow orbital drift -----------
  useEffect(() => {
    if (sceneConfigured.current || graphData.nodes.length === 0) return;
    const fg = fgRef.current;
    if (!fg) return;
    let cancelled = false;

    const configure = () => {
      if (cancelled || !fgRef.current) return;
      const g = fgRef.current;
      try {
        // Bloom — deliberately SUBTLE. Most of each node's glow comes from its own
        // additive halo mesh (see nodeThreeObject), so bloom only needs to lift the
        // very brightest cores. High threshold + low strength prevents the
        // zoomed-in white-out.
        const composer = g.postProcessingComposer?.();
        if (composer) {
          const bloom = new UnrealBloomPass(
            new THREE.Vector2(dimensions.width, dimensions.height),
            0.42, // strength (was 1.6)
            0.5, // radius
            0.62, // threshold — only the hottest pixels bloom (was 0.08)
          );
          composer.addPass(bloom);
        }
        // Starfield + light fog — depth, so it feels like an abyss not a diagram.
        const scene = g.scene?.();
        if (scene) {
          scene.fog = new THREE.FogExp2(0x05060f, 0.00065);
          const STAR_COUNT = 1400;
          const positions = new Float32Array(STAR_COUNT * 3);
          for (let i = 0; i < STAR_COUNT; i++) {
            const r = 1200 + Math.random() * 2600;
            const theta = Math.random() * Math.PI * 2;
            const phi = Math.acos(2 * Math.random() - 1);
            positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
            positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
            positions[i * 3 + 2] = r * Math.cos(phi);
          }
          const geo = new THREE.BufferGeometry();
          geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
          const mat = new THREE.PointsMaterial({ color: 0x9bb0ff, size: 2.2, transparent: true, opacity: 0.55, sizeAttenuation: true });
          scene.add(new THREE.Points(geo, mat));
        }
        // Slow auto-rotate — the "floating through your mind" drift.
        const controls = g.controls?.();
        if (controls) {
          controls.autoRotate = true;
          controls.autoRotateSpeed = 0.55;
          controls.enableDamping = true;
          controls.dampingFactor = 0.12;
        }
        sceneConfigured.current = true;
      } catch {
        // If WebGL/postprocessing isn't available, the base graph still renders.
      }
    };

    // Give the renderer a beat to initialize its composer/scene.
    const t = window.setTimeout(configure, 120);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [graphData.nodes.length, dimensions.width, dimensions.height]);

  // ---- Node click → fetch the actual memories ----------------------------
  const handleNodeClick = useCallback((node: UniNode) => {
    setSelectedNode((prev) => (prev?.id === node.id ? null : node));
    const fg = fgRef.current;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const n = node as any;
    if (fg && typeof n.x === 'number') {
      const dist = 120;
      const ratio = 1 + dist / Math.hypot(n.x || 1, n.y || 1, n.z || 1);
      fg.cameraPosition({ x: (n.x || 0) * ratio, y: (n.y || 0) * ratio, z: (n.z || 0) * ratio }, n, 1200);
    }
  }, []);

  useEffect(() => {
    if (!selectedNode) {
      setNodeMemories([]);
      setMemoriesTotal(0);
      setMemoriesError(false);
      return;
    }
    const entityId = selectedNode.id;
    let cancelled = false;
    setMemoriesLoading(true);
    setMemoriesError(false);
    setNodeMemories([]);
    api
      .getEntityMemories(entityId, 25, projectId || undefined)
      .then((res) => {
        if (cancelled) return;
        setNodeMemories(res.memories);
        setMemoriesTotal(res.total);
      })
      .catch(() => {
        if (cancelled) return;
        setNodeMemories([]);
        setMemoriesTotal(0);
        setMemoriesError(true);
      })
      .finally(() => {
        if (!cancelled) setMemoriesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedNode, projectId, memoriesRetry]);

  const entityTypes = useMemo(() => {
    const t = new Set(graphData.nodes.map((n) => n.type));
    return Array.from(t).sort();
  }, [graphData.nodes]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[600px] bg-[#05060f] rounded-[30px]">
        <div className="text-center">
          <Sparkles className="w-8 h-8 animate-pulse text-purple-400 mx-auto mb-3" />
          <span className="text-purple-200/70">Igniting the memory universe…</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-[600px] bg-red-900/20 rounded-[30px]">
        <span className="text-red-400">{error}</span>
        <button onClick={fetchGraphData} className="ml-4 px-3 py-1 bg-red-600 rounded text-white text-sm">
          Retry
        </button>
      </div>
    );
  }

  if (graphData.nodes.length === 0) {
    return (
      <div className="flex items-center justify-center h-[600px] bg-[#05060f] rounded-[30px]">
        <div className="text-center">
          <Sparkles className="w-12 h-12 text-purple-400/50 mx-auto mb-4" />
          <p className="text-white text-lg font-medium mb-2">Your universe is empty</p>
          <p className="text-purple-200/50 text-sm">Store some memories to watch your knowledge come alive</p>
        </div>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="relative isolate w-full overflow-hidden rounded-[30px]"
      style={{ height: 'clamp(680px, calc(100vh - 11rem), 880px)', background: 'radial-gradient(ellipse at center, #0a0a1f 0%, #03030a 70%)' }}
    >
      {/* Controls */}
      <div className="absolute top-0 left-0 right-0 z-10 p-4 bg-gradient-to-b from-black/70 via-black/20 to-transparent pointer-events-none">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-xs text-purple-200/80">
            <Sparkles className="w-3.5 h-3.5 text-purple-300" />
            <span><strong className="text-white">{graphData.nodes.length}</strong> entities</span>
            <span className="text-white/30">·</span>
            <span><strong className="text-white">{graphData.links.length}</strong> connections</span>
          </div>
          <button
            onClick={fetchGraphData}
            className="pointer-events-auto p-2 bg-black/40 hover:bg-black/60 rounded-lg text-white transition-colors"
            title="Refresh"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Legend */}
      <div className="absolute bottom-4 left-4 z-10 flex flex-wrap gap-3 max-w-[60%]">
        {entityTypes.slice(0, 6).map((t) => (
          <div key={t} className="flex items-center gap-1.5">
            <div className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: colorForType(t), boxShadow: `0 0 8px ${colorForType(t)}` }} />
            <span className="text-[11px] text-purple-100/60 capitalize">{t}</span>
          </div>
        ))}
      </div>

      {/* Selected entity → its real memories */}
      {selectedNode && (
        <div className="absolute top-16 right-4 z-10 w-80 max-h-[calc(100vh-9rem)] overflow-y-auto bg-black/55 backdrop-blur-xl rounded-2xl p-5 border border-white/10 shadow-2xl">
          <div className="flex items-start justify-between mb-4">
            <div>
              <h3 className="font-semibold text-white text-lg tracking-tight">{selectedNode.name}</h3>
              <div className="flex items-center gap-2 mt-1">
                <div className="w-2 h-2 rounded-full" style={{ backgroundColor: selectedNode.color, boxShadow: `0 0 8px ${selectedNode.color}` }} />
                <p className="text-xs font-medium uppercase tracking-wider" style={{ color: selectedNode.color }}>{selectedNode.type}</p>
              </div>
            </div>
            <button onClick={() => setSelectedNode(null)} className="p-1.5 rounded-md text-gray-400 hover:text-white hover:bg-white/10 transition-colors">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6 6 18" /><path d="m6 6 12 12" /></svg>
            </button>
          </div>

          <div className="mt-1 border-t border-white/5 pt-3">
            <div className="flex items-center justify-between mb-2.5">
              <span className="text-xs font-semibold uppercase tracking-wider text-gray-400">Memories</span>
              {memoriesTotal > nodeMemories.length && (
                <span className="text-[10px] text-gray-500">showing {nodeMemories.length} of {memoriesTotal}</span>
              )}
            </div>
            {memoriesLoading ? (
              <div className="flex items-center justify-center gap-2 text-xs text-gray-500 py-4">
                <Sparkles className="w-3.5 h-3.5 animate-pulse text-purple-400" />
                Loading memories…
              </div>
            ) : memoriesError ? (
              <div className="text-xs text-red-400/80 py-4 text-center">
                Couldn't load memories.{' '}
                <button onClick={() => setMemoriesRetry((n) => n + 1)} className="underline hover:text-red-300 transition-colors">Retry</button>
              </div>
            ) : nodeMemories.length === 0 ? (
              <div className="text-xs text-gray-500 py-4 text-center">No linked memories</div>
            ) : (
              <div className="space-y-2 max-h-72 overflow-y-auto pr-1 -mr-1">
                {nodeMemories.map((m) => (
                  <div key={m.id} title={m.content} className="rounded-lg bg-white/[0.04] hover:bg-white/[0.08] border border-white/5 px-3 py-2 transition-colors">
                    <p className="text-xs text-gray-200 leading-snug line-clamp-3">{m.content}</p>
                    <p className="mt-1.5 text-[10px] text-gray-500">
                      {new Date(m.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* The universe */}
      <ForceGraph3D
        ref={fgRef}
        graphData={graphData}
        width={dimensions.width}
        height={dimensions.height}
        backgroundColor="rgba(0,0,0,0)"
        showNavInfo={false}
        nodeRelSize={4}
        nodeVal={(n: UniNode) => n.val}
        nodeThreeObject={(n: object) => makeNodeObject(n as UniNode)}
        nodeThreeObjectExtend={false}
        nodeLabel={(n: UniNode) => `${n.name} · ${n.memoryCount} memories`}
        linkColor={() => 'rgba(150,160,255,0.16)'}
        linkOpacity={0.25}
        linkWidth={0.3}
        linkDirectionalParticles={(l: UniLink) => {
          const s = graphData.nodes.find((n) => n.id === (typeof l.source === 'object' ? (l.source as UniNode).id : l.source));
          return Math.min(3, 1 + Math.floor((s?.memoryCount || 1) / 4));
        }}
        linkDirectionalParticleWidth={1.1}
        linkDirectionalParticleSpeed={0.005}
        linkDirectionalParticleColor={() => 'rgba(168,85,247,0.8)'}
        onNodeClick={(n: object) => handleNodeClick(n as UniNode)}
        enableNodeDrag={false}
        warmupTicks={60}
        cooldownTicks={200}
      />
    </div>
  );
}
