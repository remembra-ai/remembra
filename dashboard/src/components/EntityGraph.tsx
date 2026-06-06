import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import type { ForceGraphMethods, NodeObject, LinkObject } from 'react-force-graph-2d';
import { api, type EntityMemoriesResponse } from '../lib/api';
import { RefreshCw, ZoomIn, ZoomOut, Maximize2, Search, Sparkles } from 'lucide-react';
import clsx from 'clsx';

interface EntityGraphProps {
  projectId?: string;
}

interface GraphNode extends NodeObject {
  id: string;
  name: string;
  type: string;
  memoryCount: number;
  color?: string;
  size?: number;
  x?: number;
  y?: number;
}

interface GraphLink extends LinkObject {
  source: string | GraphNode;
  target: string | GraphNode;
  type: string;
  color?: string;
}

const TYPE_COLORS: Record<string, string> = {
  person: '#3b82f6',       // blue
  persons: '#3b82f6',
  organization: '#a855f7', // purple
  organizations: '#a855f7',
  orgs: '#a855f7',
  company: '#a855f7',
  location: '#22c55e',     // green
  locations: '#22c55e',
  place: '#22c55e',
  places: '#22c55e',
  concept: '#f59e0b',      // amber
  concepts: '#f59e0b',
  money: '#10b981',        // emerald
  moneys: '#10b981',
  date: '#06b6d4',         // cyan
  dates: '#06b6d4',
  event: '#ec4899',        // pink
  events: '#ec4899',
  product: '#8b5cf6',      // violet
  products: '#8b5cf6',
  technology: '#14b8a6',   // teal
  technologies: '#14b8a6',
};

function graphNodeId(node: string | number | NodeObject | undefined) {
  if (node && typeof node === 'object') return String(node.id ?? '');
  return String(node ?? '');
}

export function EntityGraph({ projectId }: EntityGraphProps) {
  const graphRef = useRef<ForceGraphMethods>(null!);
  const containerRef = useRef<HTMLDivElement>(null);
  const [graphData, setGraphData] = useState<{ nodes: GraphNode[]; links: GraphLink[] }>({ nodes: [], links: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set());
  const [hoveredNode, setHoveredNode] = useState<GraphNode | null>(null);
  const [hoveredPosition, setHoveredPosition] = useState<{ x: number; y: number } | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [nodeMemories, setNodeMemories] = useState<EntityMemoriesResponse['memories']>([]);
  const [memoriesTotal, setMemoriesTotal] = useState(0);
  const [memoriesLoading, setMemoriesLoading] = useState(false);
  const [dimensions, setDimensions] = useState({ width: 960, height: 720 });
  const [highlightNodes, setHighlightNodes] = useState<Set<string>>(new Set());
  const [highlightLinks, setHighlightLinks] = useState<Set<string>>(new Set());
  const [pulsePhase, setPulsePhase] = useState(0);

  // Pulse animation for high-memory nodes
  useEffect(() => {
    const interval = setInterval(() => {
      setPulsePhase(p => (p + 0.04) % (2 * Math.PI));
    }, 80);
    return () => clearInterval(interval);
  }, []);

  // Observe the actual graph frame instead of only window resize so tab/layout changes re-center correctly.
  useEffect(() => {
    const element = containerRef.current;

    if (!element) {
      return;
    }

    const updateDimensions = (width: number, height: number) => {
      setDimensions(prev => {
        const next = {
          width: Math.max(Math.floor(width), 320),
          height: Math.max(Math.floor(height), 680),
        };

        if (prev.width === next.width && prev.height === next.height) {
          return prev;
        }

        return next;
      });
    };

    const resizeObserver = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) {
        return;
      }

      updateDimensions(entry.contentRect.width, entry.contentRect.height);
    });

    resizeObserver.observe(element);

    const rect = element.getBoundingClientRect();
    updateDimensions(rect.width, rect.height);

    return () => resizeObserver.disconnect();
  }, []);

  const fetchGraphData = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getEntityGraph(projectId || undefined);
      
      const nodes: GraphNode[] = data.nodes.map((n) => ({
        id: n.id,
        name: n.label,
        type: n.type.toLowerCase(),
        memoryCount: n.memory_count || 1,
        color: TYPE_COLORS[n.type.toLowerCase()] || '#6b7280',
        size: 4 + Math.min((n.memory_count || 1) * 2, 16),
      }));
      
      const nodeIds = new Set(nodes.map(n => n.id));
      const links: GraphLink[] = data.edges
        .filter(e => nodeIds.has(e.source) && nodeIds.has(e.target))
        .map(e => ({
          source: e.source,
          target: e.target,
          type: e.type || 'related',
          color: 'rgba(255,255,255,0.15)',
        }));
      
      setGraphData({ nodes, links });
      setSelectedNode(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load graph');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchGraphData();
  }, [projectId]);

  // Filter nodes based on search and type selection
  const filteredData = useMemo(() => {
    let nodes = graphData.nodes;
    let links = graphData.links;
    
    // Filter by search term
    if (searchTerm) {
      const term = searchTerm.toLowerCase();
      nodes = nodes.filter(n => n.name.toLowerCase().includes(term));
    }
    
    // Filter by selected types
    if (selectedTypes.size > 0) {
      nodes = nodes.filter(n => selectedTypes.has(n.type));
    }
    
    // Only include links where both endpoints exist
    const nodeIds = new Set(nodes.map(n => n.id));
    links = links.filter(l => {
      const sourceId = typeof l.source === 'object' ? l.source.id : l.source;
      const targetId = typeof l.target === 'object' ? l.target.id : l.target;
      return nodeIds.has(sourceId as string) && nodeIds.has(targetId as string);
    });
    
    return { nodes, links };
  }, [graphData, searchTerm, selectedTypes]);

  // Get unique entity types for filter
  const entityTypes = useMemo(() => {
    const types = new Set(graphData.nodes.map(n => n.type));
    return Array.from(types).sort();
  }, [graphData.nodes]);

  const fitGraphToViewport = useCallback((duration = 700) => {
    if (!graphRef.current || filteredData.nodes.length === 0) {
      return;
    }

    const padding = Math.max(80, Math.min(dimensions.width * 0.1, 140));
    graphRef.current.zoomToFit(duration, padding);
  }, [dimensions.width, filteredData.nodes.length]);

  const updateHoveredPosition = useCallback((node: GraphNode | null) => {
    if (
      !node ||
      !graphRef.current ||
      node.x === undefined ||
      node.y === undefined ||
      !isFinite(node.x) ||
      !isFinite(node.y)
    ) {
      setHoveredPosition(null);
      return;
    }

    const { x, y } = graphRef.current.graph2ScreenCoords(node.x, node.y);

    setHoveredPosition({
      x: Math.max(16, Math.min(x + 18, dimensions.width - 220)),
      y: Math.max(16, Math.min(y + 18, dimensions.height - 110)),
    });
  }, [dimensions.height, dimensions.width]);

  // Handle node hover - highlight connected nodes
  const handleNodeHover = useCallback((node: GraphNode | null) => {
    setHoveredNode(node);
    updateHoveredPosition(node);
    
    if (node) {
      const connectedNodes = new Set<string>();
      const connectedLinks = new Set<string>();
      
      connectedNodes.add(node.id);
      
      graphData.links.forEach(link => {
        const sourceId = typeof link.source === 'object' ? link.source.id : link.source;
        const targetId = typeof link.target === 'object' ? link.target.id : link.target;
        
        if (sourceId === node.id) {
          connectedNodes.add(targetId as string);
          connectedLinks.add(`${sourceId}-${targetId}`);
        } else if (targetId === node.id) {
          connectedNodes.add(sourceId as string);
          connectedLinks.add(`${sourceId}-${targetId}`);
        }
      });
      
      setHighlightNodes(connectedNodes);
      setHighlightLinks(connectedLinks);
    } else {
      setHighlightNodes(new Set());
      setHighlightLinks(new Set());
    }
  }, [graphData.links, updateHoveredPosition]);

  // Handle node click - select and zoom
  const handleNodeClick = useCallback((node: GraphNode) => {
    setSelectedNode(prev => prev?.id === node.id ? null : node);

    if (graphRef.current) {
      graphRef.current.centerAt(node.x, node.y, 500);
      graphRef.current.zoom(2, 500);
    }
  }, []);

  // When a node is selected, load the ACTUAL memories linked to that entity so
  // the panel shows what the memories are — not just a count.
  useEffect(() => {
    if (!selectedNode) {
      setNodeMemories([]);
      setMemoriesTotal(0);
      return;
    }
    let cancelled = false;
    setMemoriesLoading(true);
    setNodeMemories([]);
    api.getEntityMemories(selectedNode.id, 25)
      .then((res) => {
        if (cancelled) return;
        setNodeMemories(res.memories);
        setMemoriesTotal(res.total);
      })
      .catch(() => {
        if (cancelled) return;
        setNodeMemories([]);
        setMemoriesTotal(0);
      })
      .finally(() => {
        if (!cancelled) setMemoriesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedNode]);

  useEffect(() => {
    if (!hoveredNode) {
      setHoveredPosition(null);
      return;
    }

    const interval = window.setInterval(() => updateHoveredPosition(hoveredNode), 50);
    return () => window.clearInterval(interval);
  }, [hoveredNode, updateHoveredPosition]);

  useEffect(() => {
    const graph = graphRef.current;

    if (!graph || filteredData.nodes.length === 0) {
      return;
    }

    const nodeLookup = new Map(filteredData.nodes.map(node => [node.id, node]));
    const chargeForce = graph.d3Force('charge') as
      | {
          strength?: (value: number) => unknown;
          distanceMax?: (value: number) => unknown;
        }
      | undefined;
    const linkForce = graph.d3Force('link') as
      | {
          distance?: (distance: ((link: GraphLink) => number) | number) => unknown;
          strength?: (strength: ((link: GraphLink) => number) | number) => unknown;
          iterations?: (count: number) => unknown;
        }
      | undefined;

    chargeForce?.strength?.(filteredData.nodes.length > 180 ? -42 : -58);
    chargeForce?.distanceMax?.(Math.max(dimensions.width * 0.42, 320));
    linkForce?.distance?.((link: GraphLink) => {
      const sourceId = typeof link.source === 'object' ? link.source.id : link.source;
      const targetId = typeof link.target === 'object' ? link.target.id : link.target;
      const source = nodeLookup.get(sourceId as string);
      const target = nodeLookup.get(targetId as string);
      const density = (source?.memoryCount || 1) + (target?.memoryCount || 1);

      return Math.max(36, 72 - Math.min(density * 1.8, 28));
    });
    linkForce?.strength?.((link: GraphLink) => {
      const sourceId = typeof link.source === 'object' ? link.source.id : link.source;
      const targetId = typeof link.target === 'object' ? link.target.id : link.target;
      const source = nodeLookup.get(sourceId as string);
      const target = nodeLookup.get(targetId as string);
      const density = (source?.memoryCount || 1) + (target?.memoryCount || 1);

      return Math.min(0.28, 0.08 + density * 0.01);
    });
    linkForce?.iterations?.(2);

    graph.d3ReheatSimulation();

    const timeout = window.setTimeout(() => fitGraphToViewport(), 360);
    return () => window.clearTimeout(timeout);
  }, [dimensions.height, dimensions.width, filteredData.links, filteredData.nodes, fitGraphToViewport]);

  useEffect(() => {
    if (filteredData.nodes.length === 0) {
      return;
    }

    const interval = window.setInterval(() => {
      if (document.hidden) {
        return;
      }

      graphRef.current?.d3ReheatSimulation();
    }, 6500);

    return () => window.clearInterval(interval);
  }, [filteredData.nodes.length]);

  // Custom node rendering
  const paintNode = useCallback((node: GraphNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
    // Guard against invalid coordinates
    if (node.x === undefined || node.y === undefined || !isFinite(node.x) || !isFinite(node.y)) {
      return;
    }
    
    const isHighlighted = highlightNodes.has(node.id);
    const isSelected = selectedNode?.id === node.id;
    const label = node.name;
    const fontSize = Math.max(12 / globalScale, 3);
    const nodeSize = node.size || 6;
    
    // ALWAYS have subtle ambient glow - circuit board aesthetic
    ctx.shadowColor = node.color || '#6b7280';
    ctx.shadowBlur = isHighlighted || isSelected ? 25 : 8;
    
    // Modern flat gradient for the node core
    const gradient = ctx.createRadialGradient(
      node.x, node.y, 0,
      node.x, node.y, nodeSize
    );
    // Lighter center, matching color at edge
    gradient.addColorStop(0, adjustColor(node.color || '#6b7280', 40));
    gradient.addColorStop(1, node.color || '#6b7280');
    
    ctx.beginPath();
    ctx.arc(node.x, node.y, nodeSize, 0, 2 * Math.PI);
    ctx.fillStyle = gradient;
    ctx.fill();
    
    // Add outer ring glow for highlighted/selected nodes
    if (isHighlighted || isSelected) {
      ctx.beginPath();
      ctx.arc(node.x, node.y, nodeSize + 4, 0, 2 * Math.PI);
      ctx.strokeStyle = `${node.color}40`; // 25% opacity
      ctx.lineWidth = 3;
      ctx.stroke();
    }
    
    // Pulsing outer ring for high-memory nodes (circuit board data flow)
    if (node.memoryCount > 5) {
      const pulseRadius = nodeSize + 6 + Math.sin(pulsePhase) * 2;
      ctx.beginPath();
      ctx.arc(node.x, node.y, pulseRadius, 0, 2 * Math.PI);
      ctx.strokeStyle = `${node.color}30`;
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    
    // Reset shadow
    ctx.shadowBlur = 0;
    
    // Draw label
    if (globalScale > 0.5 || isHighlighted) {
      ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      
      // Premium Glassmorphic Label Background
      const textWidth = ctx.measureText(label).width;
      const paddingX = 6 / globalScale;
      const paddingY = 3 / globalScale;
      
      // Draw rounded rectangle for label background
      const rectX = node.x - textWidth / 2 - paddingX;
      const rectY = node.y + nodeSize + 4 / globalScale;
      const rectWidth = textWidth + paddingX * 2;
      const rectHeight = fontSize + paddingY * 2;
      const radius = 4 / globalScale;
      
      ctx.beginPath();
      ctx.moveTo(rectX + radius, rectY);
      ctx.arcTo(rectX + rectWidth, rectY, rectX + rectWidth, rectY + rectHeight, radius);
      ctx.arcTo(rectX + rectWidth, rectY + rectHeight, rectX, rectY + rectHeight, radius);
      ctx.arcTo(rectX, rectY + rectHeight, rectX, rectY, radius);
      ctx.arcTo(rectX, rectY, rectX + rectWidth, rectY, radius);
      ctx.closePath();
      
      ctx.fillStyle = isHighlighted ? 'rgba(0,0,0,0.85)' : 'rgba(0,0,0,0.6)';
      ctx.fill();
      
      // Subtle border
      ctx.strokeStyle = isHighlighted ? 'rgba(255,255,255,0.2)' : 'rgba(255,255,255,0.05)';
      ctx.lineWidth = 1 / globalScale;
      ctx.stroke();
      
      // Label text
      ctx.fillStyle = isHighlighted ? '#ffffff' : 'rgba(255,255,255,0.7)';
      ctx.fillText(label, node.x, rectY + paddingY + 1 / globalScale);
    }
  }, [highlightNodes, selectedNode]);

  // Custom link rendering
  const paintLink = useCallback((link: GraphLink, ctx: CanvasRenderingContext2D, globalScale: number) => {
    const source = link.source as GraphNode;
    const target = link.target as GraphNode;
    
    // Guard against invalid coordinates
    if (source.x === undefined || source.y === undefined || 
        target.x === undefined || target.y === undefined ||
        !isFinite(source.x) || !isFinite(source.y) ||
        !isFinite(target.x) || !isFinite(target.y)) {
      return;
    }
    
    const linkId = `${source.id}-${target.id}`;
    const isHighlighted = highlightLinks.has(linkId);
    
    ctx.beginPath();
    ctx.moveTo(source.x, source.y);
    ctx.lineTo(target.x, target.y);
    
    if (isHighlighted) {
      // Circuit-trace glow for highlighted links
      ctx.shadowColor = 'rgba(168, 85, 247, 0.8)';
      ctx.shadowBlur = 10;
      ctx.strokeStyle = 'rgba(168, 85, 247, 0.9)';
      ctx.lineWidth = 3 / globalScale;
    } else {
      // Gradient stroke with a living shimmer so the graph feels active rather than frozen.
      const shimmer = 0.12 + ((Math.sin(pulsePhase + source.memoryCount * 0.35) + 1) / 2) * 0.14;
      const gradient = ctx.createLinearGradient(source.x, source.y, target.x, target.y);
      gradient.addColorStop(0, `rgba(${hexToRgb(source.color || '#a855f7')}, ${shimmer})`);
      gradient.addColorStop(0.5, `rgba(168, 85, 247, ${Math.min(shimmer + 0.06, 0.34)})`);
      gradient.addColorStop(1, `rgba(${hexToRgb(target.color || '#a855f7')}, ${shimmer})`);
      
      ctx.shadowBlur = 0;
      ctx.strokeStyle = gradient;
      ctx.lineWidth = 1.6 / globalScale;
    }
    
    ctx.stroke();
    ctx.shadowBlur = 0; // Reset shadow for other drawn elements
    
    // Draw relationship label on hover with sleek background
    if (isHighlighted && link.type && globalScale > 1) {
      const midX = (source.x + target.x) / 2;
      const midY = (source.y + target.y) / 2;
      
      const fontSize = 10 / globalScale;
      ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      
      const textWidth = ctx.measureText(link.type).width;
      const px = 4 / globalScale;
      const py = 2 / globalScale;
      
      ctx.fillStyle = 'rgba(0,0,0,0.8)';
      ctx.fillRect(midX - textWidth/2 - px, midY - fontSize/2 - py, textWidth + px*2, fontSize + py*2);
      
      ctx.fillStyle = 'rgba(255,255,255,0.9)';
      ctx.fillText(link.type, midX, midY);
    }
  }, [highlightLinks, pulsePhase]);

  // Zoom controls
  const handleZoomIn = () => graphRef.current?.zoom(graphRef.current.zoom() * 1.5, 300);
  const handleZoomOut = () => graphRef.current?.zoom(graphRef.current.zoom() / 1.5, 300);
  const handleReset = () => {
    fitGraphToViewport(450);
    setSelectedNode(null);
  };

  // Toggle type filter
  const toggleType = (type: string) => {
    setSelectedTypes(prev => {
      const next = new Set(prev);
      if (next.has(type)) {
        next.delete(type);
      } else {
        next.add(type);
      }
      return next;
    });
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[600px] bg-[hsl(var(--card))] rounded-lg">
        <div className="text-center">
          <Sparkles className="w-8 h-8 animate-pulse text-purple-400 mx-auto mb-3" />
          <span className="text-[hsl(var(--muted-foreground))]">Building knowledge graph...</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-[600px] bg-red-900/20 rounded-lg">
        <span className="text-red-400">{error}</span>
        <button onClick={fetchGraphData} className="ml-4 px-3 py-1 bg-red-600 rounded text-white text-sm">
          Retry
        </button>
      </div>
    );
  }

  if (graphData.nodes.length === 0) {
    return (
      <div className="flex items-center justify-center h-[600px] bg-[hsl(var(--card))] rounded-lg">
        <div className="text-center">
          <Sparkles className="w-12 h-12 text-purple-400/50 mx-auto mb-4" />
          <p className="text-[hsl(var(--foreground))] text-lg font-medium mb-2">No entities to visualize</p>
          <p className="text-[hsl(var(--muted-foreground))] text-sm">Store some memories to see your knowledge graph come alive</p>
        </div>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="dashboard-surface relative isolate w-full overflow-hidden rounded-[30px]"
      style={{ height: 'clamp(680px, calc(100vh - 11rem), 860px)' }}
    >
      {/* Depth background with radial glow - circuit board aesthetic */}
      <div className="absolute inset-0 bg-[#0a0a0f]">
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,_rgba(168,85,247,0.18)_0%,_transparent_68%)]" />
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_top_right,_rgba(59,130,246,0.12)_0%,_transparent_52%)]" />
        <div className="absolute inset-0 opacity-35" style={{
          backgroundImage: 'radial-gradient(circle at 25% 25%, rgba(168,85,247,0.1) 0%, transparent 50%)',
        }} />
        <div
          className="absolute inset-0 opacity-20"
          style={{
            backgroundImage: `
              linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
              linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px)
            `,
            backgroundSize: '84px 84px',
          }}
        />
      </div>
      {/* Top Controls Bar */}
      <div className="absolute top-0 left-0 right-0 z-10 p-4 bg-gradient-to-b from-black/70 via-black/20 to-transparent">
        <div className="flex items-center justify-between gap-4">
          {/* Search */}
          <div className="relative flex-1 max-w-xs">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
            <input
              type="text"
              placeholder="Search entities..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="w-full pl-10 pr-4 py-2.5 bg-black/45 border border-white/10 rounded-xl text-white placeholder-gray-400 text-sm focus:outline-none focus:border-purple-500 focus:ring-2 focus:ring-purple-400/20"
            />
          </div>
          
          {/* Type Filters */}
          <div className="flex items-center gap-1 flex-wrap">
            {entityTypes.slice(0, 6).map(type => (
              <button
                key={type}
                onClick={() => toggleType(type)}
                className={clsx(
                  'px-2 py-1 rounded-full text-xs font-medium transition-all',
                  selectedTypes.has(type)
                    ? 'bg-white/20 text-white ring-1 ring-white/40'
                    : 'bg-black/30 text-gray-300 hover:bg-black/50'
                )}
                style={{ 
                  borderLeft: `3px solid ${TYPE_COLORS[type] || '#6b7280'}` 
                }}
              >
                {type}
              </button>
            ))}
            {selectedTypes.size > 0 && (
              <button
                onClick={() => setSelectedTypes(new Set())}
                className="px-2 py-1 text-xs text-gray-400 hover:text-white"
              >
                Clear
              </button>
            )}
          </div>
          
          {/* Zoom Controls */}
          <div className="flex items-center gap-1">
            <button
              onClick={handleZoomIn}
              className="p-2 bg-black/40 hover:bg-black/60 rounded-lg text-white transition-colors"
              title="Zoom In"
            >
              <ZoomIn className="w-4 h-4" />
            </button>
            <button
              onClick={handleZoomOut}
              className="p-2 bg-black/40 hover:bg-black/60 rounded-lg text-white transition-colors"
              title="Zoom Out"
            >
              <ZoomOut className="w-4 h-4" />
            </button>
            <button
              onClick={handleReset}
              className="p-2 bg-black/40 hover:bg-black/60 rounded-lg text-white transition-colors"
              title="Fit to View"
            >
              <Maximize2 className="w-4 h-4" />
            </button>
            <button
              onClick={fetchGraphData}
              className="p-2 bg-black/40 hover:bg-black/60 rounded-lg text-white transition-colors"
              title="Refresh"
            >
              <RefreshCw className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>

      {/* Stats Bar */}
      <div className="absolute bottom-0 left-0 right-0 z-10 p-4 bg-gradient-to-t from-black/80 via-black/28 to-transparent">
        <div className="flex items-center justify-between text-xs">
          <div className="flex items-center gap-4 text-gray-300">
            <span><strong className="text-white">{filteredData.nodes.length}</strong> entities</span>
            <span><strong className="text-white">{filteredData.links.length}</strong> relationships</span>
          </div>
          
          {/* Legend */}
          <div className="flex items-center gap-3">
            {entityTypes.slice(0, 5).map(type => (
              <div key={type} className="flex items-center gap-1.5">
                <div 
                  className="w-2.5 h-2.5 rounded-full"
                  style={{ backgroundColor: TYPE_COLORS[type] || '#6b7280' }}
                />
                <span className="text-gray-400 capitalize">{type}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Selected Node Info */}
      {selectedNode && (
        <div className="absolute top-20 right-4 z-10 w-80 max-h-[calc(100vh-7rem)] overflow-y-auto bg-black/60 backdrop-blur-xl rounded-xl p-5 border border-white/5 shadow-2xl transition-all duration-300 animate-in fade-in slide-in-from-right-4">
          <div className="flex items-start justify-between mb-4">
            <div>
              <h3 className="font-semibold text-white text-lg tracking-tight">{selectedNode.name}</h3>
              <div className="flex items-center gap-2 mt-1">
                <div 
                  className="w-2 h-2 rounded-full shadow-[0_0_8px_rgba(255,255,255,0.5)]"
                  style={{ backgroundColor: selectedNode.color, boxShadow: `0 0 8px ${selectedNode.color}` }}
                />
                <p className="text-xs font-medium uppercase tracking-wider" style={{ color: selectedNode.color }}>
                  {selectedNode.type}
                </p>
              </div>
            </div>
            <button 
              onClick={() => setSelectedNode(null)}
              className="p-1.5 rounded-md text-gray-400 hover:text-white hover:bg-white/10 transition-colors"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
            </button>
          </div>
          <div className="space-y-3 text-sm">
            <div className="flex justify-between items-center py-2 border-t border-white/5">
              <span className="text-gray-400">Total Memories</span>
              <span className="text-white font-medium bg-white/10 px-2 py-0.5 rounded-md">{selectedNode.memoryCount}</span>
            </div>
            <div className="flex justify-between items-center py-2 border-t border-white/5">
              <span className="text-gray-400">Direct Connections</span>
              <span className="text-white font-medium bg-white/10 px-2 py-0.5 rounded-md">
                {graphData.links.filter(l => {
                  const sourceId = typeof l.source === 'object' ? l.source.id : l.source;
                  const targetId = typeof l.target === 'object' ? l.target.id : l.target;
                  return sourceId === selectedNode.id || targetId === selectedNode.id;
                }).length}
              </span>
            </div>
          </div>

          {/* The actual memories linked to this entity — not just a count */}
          <div className="mt-4 border-t border-white/5 pt-3">
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
            ) : nodeMemories.length === 0 ? (
              <div className="text-xs text-gray-500 py-4 text-center">No linked memories</div>
            ) : (
              <div className="space-y-2 max-h-72 overflow-y-auto pr-1 -mr-1">
                {nodeMemories.map((m) => (
                  <div
                    key={m.id}
                    title={m.content}
                    className="rounded-lg bg-white/[0.04] hover:bg-white/[0.08] border border-white/5 px-3 py-2 transition-colors"
                  >
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

      {/* Hover Tooltip */}
      {hoveredNode && !selectedNode && (
        <div 
          className="absolute z-20 px-4 py-3 bg-black/70 backdrop-blur-md rounded-xl text-sm pointer-events-none border border-white/10 shadow-xl transition-all duration-150"
          style={{
            left: hoveredPosition?.x ?? 20,
            top: hoveredPosition?.y ?? 20,
          }}
        >
          <div className="font-semibold text-white tracking-tight">{hoveredNode.name}</div>
          <div className="flex items-center gap-2 mt-1.5">
            <div 
              className="w-1.5 h-1.5 rounded-full"
              style={{ backgroundColor: hoveredNode.color, boxShadow: `0 0 4px ${hoveredNode.color}` }}
            />
            <div className="text-[11px] font-medium uppercase tracking-wider text-gray-300">
              {hoveredNode.type}
            </div>
          </div>
          <div className="mt-2 text-xs text-gray-400">{hoveredNode.memoryCount} referenced memories</div>
        </div>
      )}

      {/* Force Graph */}
      <ForceGraph2D
        ref={graphRef}
        graphData={filteredData}
        width={dimensions.width}
        height={dimensions.height}
        backgroundColor="transparent"
        nodeCanvasObject={paintNode}
        nodePointerAreaPaint={(node, color, ctx) => {
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(node.x, node.y, (node as GraphNode).size || 6, 0, 2 * Math.PI);
          ctx.fill();
        }}
        linkCanvasObject={paintLink}
        linkDirectionalParticles={(link) => highlightLinks.has(`${graphNodeId((link as GraphLink).source)}-${graphNodeId((link as GraphLink).target)}`) ? 4 : 1}
        linkDirectionalParticleWidth={(link) => highlightLinks.has(`${graphNodeId((link as GraphLink).source)}-${graphNodeId((link as GraphLink).target)}`) ? 2.4 : 1.2}
        linkDirectionalParticleSpeed={(link) => highlightLinks.has(`${graphNodeId((link as GraphLink).source)}-${graphNodeId((link as GraphLink).target)}`) ? 0.013 : 0.0045}
        linkDirectionalParticleColor={(link) => {
          const source = (link as GraphLink).source as GraphNode;
          return source.color || 'rgba(168, 85, 247, 0.8)';
        }}
        onNodeHover={(node) => handleNodeHover(node as GraphNode | null)}
        onNodeClick={(node) => handleNodeClick(node as GraphNode)}
        cooldownTicks={Infinity}
        d3AlphaDecay={0.008}
        d3VelocityDecay={0.2}
        warmupTicks={100}
        d3AlphaMin={0.001}
        autoPauseRedraw={false}
        enableNodeDrag={true}
        enableZoomInteraction={true}
        enablePanInteraction={true}
      />
    </div>
  );
}

// Helper to darken/lighten colors
function adjustColor(color: string, amount: number): string {
  const hex = color.replace('#', '');
  const r = Math.max(0, Math.min(255, parseInt(hex.slice(0, 2), 16) + amount));
  const g = Math.max(0, Math.min(255, parseInt(hex.slice(2, 4), 16) + amount));
  const b = Math.max(0, Math.min(255, parseInt(hex.slice(4, 6), 16) + amount));
  return `rgb(${r},${g},${b})`;
}

// Helper to convert hex to rgb string format
function hexToRgb(hex: string): string {
  const cleanHex = hex.replace('#', '');
  const r = parseInt(cleanHex.slice(0, 2), 16);
  const g = parseInt(cleanHex.slice(2, 4), 16);
  const b = parseInt(cleanHex.slice(4, 6), 16);
  return `${r}, ${g}, ${b}`;
}
