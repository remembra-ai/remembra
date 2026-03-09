import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import type { ForceGraphMethods, NodeObject, LinkObject } from 'react-force-graph-2d';
import { api } from '../lib/api';
import { RefreshCw, ZoomIn, ZoomOut, Maximize2, Search, Filter, Sparkles } from 'lucide-react';
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

const TYPE_LABELS: Record<string, string> = {
  person: '👤 Person',
  persons: '👤 Person',
  organization: '🏢 Organization',
  organizations: '🏢 Organization',
  orgs: '🏢 Organization',
  company: '🏢 Company',
  location: '📍 Location',
  locations: '📍 Location',
  place: '📍 Place',
  places: '📍 Place',
  concept: '💡 Concept',
  concepts: '💡 Concept',
  money: '💰 Money',
  moneys: '💰 Money',
  date: '📅 Date',
  dates: '📅 Date',
  event: '🎉 Event',
  events: '🎉 Event',
  product: '📦 Product',
  products: '📦 Product',
  technology: '⚡ Technology',
  technologies: '⚡ Technology',
};

export function EntityGraph({ projectId }: EntityGraphProps) {
  const graphRef = useRef<ForceGraphMethods>(null!);
  const containerRef = useRef<HTMLDivElement>(null);
  const [graphData, setGraphData] = useState<{ nodes: GraphNode[]; links: GraphLink[] }>({ nodes: [], links: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set());
  const [hoveredNode, setHoveredNode] = useState<GraphNode | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [dimensions, setDimensions] = useState({ width: 900, height: 600 });
  const [highlightNodes, setHighlightNodes] = useState<Set<string>>(new Set());
  const [highlightLinks, setHighlightLinks] = useState<Set<string>>(new Set());

  // Get container dimensions
  useEffect(() => {
    const updateDimensions = () => {
      if (containerRef.current) {
        const rect = containerRef.current.getBoundingClientRect();
        setDimensions({ width: rect.width || 900, height: 600 });
      }
    };
    updateDimensions();
    window.addEventListener('resize', updateDimensions);
    return () => window.removeEventListener('resize', updateDimensions);
  }, []);

  const fetchGraphData = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getEntityGraph(projectId || 'default');
      
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

  // Handle node hover - highlight connected nodes
  const handleNodeHover = useCallback((node: GraphNode | null) => {
    setHoveredNode(node);
    
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
  }, [graphData.links]);

  // Handle node click - select and zoom
  const handleNodeClick = useCallback((node: GraphNode) => {
    setSelectedNode(prev => prev?.id === node.id ? null : node);
    
    if (graphRef.current) {
      graphRef.current.centerAt(node.x, node.y, 500);
      graphRef.current.zoom(2, 500);
    }
  }, []);

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
    
    // Glow effect for highlighted nodes
    if (isHighlighted || isSelected) {
      ctx.shadowColor = node.color || '#fff';
      ctx.shadowBlur = 15;
    }
    
    // Draw node circle with gradient
    const gradient = ctx.createRadialGradient(
      node.x, node.y, 0,
      node.x, node.y, nodeSize
    );
    gradient.addColorStop(0, node.color || '#6b7280');
    gradient.addColorStop(1, adjustColor(node.color || '#6b7280', -30));
    
    ctx.beginPath();
    ctx.arc(node.x, node.y, nodeSize, 0, 2 * Math.PI);
    ctx.fillStyle = gradient;
    ctx.fill();
    
    // Ring for selected node
    if (isSelected) {
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 2 / globalScale;
      ctx.stroke();
    }
    
    // Reset shadow
    ctx.shadowBlur = 0;
    
    // Draw label
    if (globalScale > 0.5 || isHighlighted) {
      ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      
      // Label background
      const textWidth = ctx.measureText(label).width;
      const padding = 2 / globalScale;
      ctx.fillStyle = 'rgba(0,0,0,0.7)';
      ctx.fillRect(
        node.x - textWidth / 2 - padding,
        node.y + nodeSize + 2,
        textWidth + padding * 2,
        fontSize + padding
      );
      
      // Label text
      ctx.fillStyle = isHighlighted ? '#fff' : 'rgba(255,255,255,0.8)';
      ctx.fillText(label, node.x, node.y + nodeSize + 3);
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
      ctx.strokeStyle = 'rgba(168, 85, 247, 0.8)';  // Purple highlight
      ctx.lineWidth = 2.5 / globalScale;
    } else {
      ctx.strokeStyle = 'rgba(255,255,255,0.25)';  // More visible base
      ctx.lineWidth = 1.5 / globalScale;
    }
    
    ctx.stroke();
    
    // Draw relationship label on hover
    if (isHighlighted && link.type && globalScale > 1) {
      const midX = (source.x + target.x) / 2;
      const midY = (source.y + target.y) / 2;
      
      ctx.font = `${10 / globalScale}px Inter, system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.fillStyle = 'rgba(255,255,255,0.8)';
      ctx.fillText(link.type, midX, midY);
    }
  }, [highlightLinks]);

  // Zoom controls
  const handleZoomIn = () => graphRef.current?.zoom(graphRef.current.zoom() * 1.5, 300);
  const handleZoomOut = () => graphRef.current?.zoom(graphRef.current.zoom() / 1.5, 300);
  const handleReset = () => {
    graphRef.current?.zoomToFit(400, 50);
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
    <div ref={containerRef} className="relative rounded-lg overflow-hidden bg-gradient-to-br from-gray-900 via-gray-900 to-purple-900/20">
      {/* Top Controls Bar */}
      <div className="absolute top-0 left-0 right-0 z-10 p-4 bg-gradient-to-b from-black/60 to-transparent">
        <div className="flex items-center justify-between gap-4">
          {/* Search */}
          <div className="relative flex-1 max-w-xs">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
            <input
              type="text"
              placeholder="Search entities..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="w-full pl-10 pr-4 py-2 bg-black/40 border border-white/10 rounded-lg text-white placeholder-gray-400 text-sm focus:outline-none focus:border-purple-500"
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
      <div className="absolute bottom-0 left-0 right-0 z-10 p-3 bg-gradient-to-t from-black/60 to-transparent">
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
        <div className="absolute top-20 right-4 z-10 w-64 bg-black/80 backdrop-blur-sm rounded-lg p-4 border border-white/10">
          <div className="flex items-start justify-between mb-3">
            <div>
              <h3 className="font-semibold text-white text-lg">{selectedNode.name}</h3>
              <p className="text-sm" style={{ color: selectedNode.color }}>
                {TYPE_LABELS[selectedNode.type] || selectedNode.type}
              </p>
            </div>
            <button 
              onClick={() => setSelectedNode(null)}
              className="text-gray-400 hover:text-white"
            >
              ✕
            </button>
          </div>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-gray-400">Memories</span>
              <span className="text-white font-medium">{selectedNode.memoryCount}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-400">Connections</span>
              <span className="text-white font-medium">
                {graphData.links.filter(l => {
                  const sourceId = typeof l.source === 'object' ? l.source.id : l.source;
                  const targetId = typeof l.target === 'object' ? l.target.id : l.target;
                  return sourceId === selectedNode.id || targetId === selectedNode.id;
                }).length}
              </span>
            </div>
          </div>
        </div>
      )}

      {/* Hover Tooltip */}
      {hoveredNode && !selectedNode && (
        <div 
          className="absolute z-20 px-3 py-2 bg-black/90 rounded-lg text-sm pointer-events-none"
          style={{
            left: Math.min((hoveredNode.x || 0) + 20, dimensions.width - 150),
            top: (hoveredNode.y || 0) + 20,
          }}
        >
          <div className="font-medium text-white">{hoveredNode.name}</div>
          <div className="text-xs" style={{ color: hoveredNode.color }}>
            {TYPE_LABELS[hoveredNode.type] || hoveredNode.type}
          </div>
          <div className="text-xs text-gray-400">{hoveredNode.memoryCount} memories</div>
        </div>
      )}

      {/* Force Graph */}
      <ForceGraph2D
        ref={graphRef}
        graphData={filteredData}
        width={dimensions.width}
        height={600}
        backgroundColor="transparent"
        nodeCanvasObject={paintNode}
        nodePointerAreaPaint={(node, color, ctx) => {
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(node.x, node.y, (node as GraphNode).size || 6, 0, 2 * Math.PI);
          ctx.fill();
        }}
        linkCanvasObject={paintLink}
        linkDirectionalParticles={2}
        linkDirectionalParticleWidth={2}
        linkDirectionalParticleSpeed={0.005}
        linkDirectionalParticleColor={() => 'rgba(168, 85, 247, 0.8)'}
        onNodeHover={handleNodeHover as any}
        onNodeClick={handleNodeClick as any}
        cooldownTicks={Infinity}
        d3AlphaDecay={0.008}
        d3VelocityDecay={0.2}
        warmupTicks={100}
        d3AlphaMin={0.001}
        onEngineStop={() => graphRef.current?.zoomToFit(400, 50)}
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
