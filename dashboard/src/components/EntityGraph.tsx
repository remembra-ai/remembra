import { useState, useEffect, useRef } from 'react';
import { api } from '../lib/api';
import type { RelationshipResponse } from '../lib/api';
import { RefreshCw, ZoomIn, ZoomOut, Maximize2 } from 'lucide-react';

interface EntityGraphProps {
  projectId?: string;
}

interface Node {
  id: string;
  name: string;
  type: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
  memoryCount: number;
}

interface Edge {
  source: string;
  target: string;
  type: string;
}

const TYPE_COLORS: Record<string, string> = {
  person: '#3b82f6',      // blue
  organization: '#8b5cf6', // purple
  company: '#8b5cf6',      // purple
  location: '#22c55e',     // green
  place: '#22c55e',        // green
  concept: '#eab308',      // yellow
};

export function EntityGraph({ projectId }: EntityGraphProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });
  const [hoveredNode, setHoveredNode] = useState<Node | null>(null);
  const animationRef = useRef<number | undefined>(undefined);

  const fetchGraphData = async () => {
    setLoading(true);
    setError(null);
    try {
      // Get all entities
      const entitiesResponse = await api.listEntities(projectId, undefined, 200);
      
      // Get relationships for each entity (batch)
      const allRelationships: RelationshipResponse[] = [];
      const seenRelIds = new Set<string>();
      
      for (const entity of entitiesResponse.entities.slice(0, 50)) { // Limit for performance
        try {
          const relResponse = await api.getEntityRelationships(entity.id);
          for (const rel of relResponse.relationships) {
            if (!seenRelIds.has(rel.id)) {
              seenRelIds.add(rel.id);
              allRelationships.push(rel);
            }
          }
        } catch {
          // Skip entities with no relationships
        }
      }
      
      // Create nodes with random initial positions
      const canvas = canvasRef.current;
      const width = canvas?.width || 800;
      const height = canvas?.height || 600;
      const centerX = width / 2;
      const centerY = height / 2;
      
      const newNodes: Node[] = entitiesResponse.entities.map((e) => ({
        id: e.id,
        name: e.canonical_name,
        type: e.type.toLowerCase(),
        x: centerX + (Math.random() - 0.5) * 300,
        y: centerY + (Math.random() - 0.5) * 300,
        vx: 0,
        vy: 0,
        memoryCount: e.memory_count,
      }));
      
      // Create edges
      const nodeIds = new Set(newNodes.map(n => n.id));
      const newEdges: Edge[] = allRelationships
        .filter(r => nodeIds.has(r.from_entity_id) && nodeIds.has(r.to_entity_id))
        .map(r => ({
          source: r.from_entity_id,
          target: r.to_entity_id,
          type: r.type,
        }));
      
      setNodes(newNodes);
      setEdges(newEdges);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load graph');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchGraphData();
  }, [projectId]);

  // Force-directed layout simulation
  useEffect(() => {
    if (nodes.length === 0) return;

    const simulate = () => {
      setNodes(prevNodes => {
        const newNodes = [...prevNodes];
        
        // Apply forces
        for (let i = 0; i < newNodes.length; i++) {
          const node = newNodes[i];
          
          // Repulsion from other nodes
          for (let j = 0; j < newNodes.length; j++) {
            if (i === j) continue;
            const other = newNodes[j];
            const dx = node.x - other.x;
            const dy = node.y - other.y;
            const dist = Math.sqrt(dx * dx + dy * dy) || 1;
            const force = 500 / (dist * dist);
            node.vx += (dx / dist) * force;
            node.vy += (dy / dist) * force;
          }
          
          // Attraction along edges
          for (const edge of edges) {
            if (edge.source === node.id || edge.target === node.id) {
              const otherId = edge.source === node.id ? edge.target : edge.source;
              const other = newNodes.find(n => n.id === otherId);
              if (other) {
                const dx = other.x - node.x;
                const dy = other.y - node.y;
                const dist = Math.sqrt(dx * dx + dy * dy) || 1;
                const force = dist * 0.01;
                node.vx += (dx / dist) * force;
                node.vy += (dy / dist) * force;
              }
            }
          }
          
          // Center gravity
          const canvas = canvasRef.current;
          if (canvas) {
            const centerX = canvas.width / 2;
            const centerY = canvas.height / 2;
            node.vx += (centerX - node.x) * 0.001;
            node.vy += (centerY - node.y) * 0.001;
          }
          
          // Apply velocity with damping
          node.vx *= 0.9;
          node.vy *= 0.9;
          node.x += node.vx;
          node.y += node.vy;
        }
        
        return newNodes;
      });
      
      animationRef.current = requestAnimationFrame(simulate);
    };
    
    animationRef.current = requestAnimationFrame(simulate);
    
    return () => {
      if (animationRef.current) {
        cancelAnimationFrame(animationRef.current);
      }
    };
  }, [edges]);

  // Render canvas
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    
    // Clear
    ctx.fillStyle = '#111827'; // dark background
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    
    ctx.save();
    ctx.translate(offset.x, offset.y);
    ctx.scale(zoom, zoom);
    
    // Draw edges
    ctx.strokeStyle = '#374151';
    ctx.lineWidth = 1;
    for (const edge of edges) {
      const source = nodes.find(n => n.id === edge.source);
      const target = nodes.find(n => n.id === edge.target);
      if (source && target) {
        ctx.beginPath();
        ctx.moveTo(source.x, source.y);
        ctx.lineTo(target.x, target.y);
        ctx.stroke();
        
        // Draw edge label
        const midX = (source.x + target.x) / 2;
        const midY = (source.y + target.y) / 2;
        ctx.fillStyle = '#6b7280';
        ctx.font = '10px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(edge.type, midX, midY);
      }
    }
    
    // Draw nodes
    for (const node of nodes) {
      const radius = 8 + Math.min(node.memoryCount * 2, 20);
      const color = TYPE_COLORS[node.type] || '#6b7280';
      
      // Node circle
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      
      // Highlight on hover
      if (hoveredNode?.id === node.id) {
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      
      // Node label
      ctx.fillStyle = '#fff';
      ctx.font = '12px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(node.name, node.x, node.y + radius + 14);
    }
    
    ctx.restore();
  }, [nodes, edges, zoom, offset, hoveredNode]);

  // Mouse handlers
  const handleMouseDown = (e: React.MouseEvent) => {
    setDragging(true);
    setDragStart({ x: e.clientX - offset.x, y: e.clientY - offset.y });
  };

  const handleMouseMove = (e: React.MouseEvent) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    
    const rect = canvas.getBoundingClientRect();
    const x = (e.clientX - rect.left - offset.x) / zoom;
    const y = (e.clientY - rect.top - offset.y) / zoom;
    
    // Check for hover
    let found: Node | null = null;
    for (const node of nodes) {
      const radius = 8 + Math.min(node.memoryCount * 2, 20);
      const dx = x - node.x;
      const dy = y - node.y;
      if (dx * dx + dy * dy < radius * radius) {
        found = node;
        break;
      }
    }
    setHoveredNode(found);
    
    if (dragging) {
      setOffset({
        x: e.clientX - dragStart.x,
        y: e.clientY - dragStart.y,
      });
    }
  };

  const handleMouseUp = () => {
    setDragging(false);
  };

  const handleZoomIn = () => setZoom(z => Math.min(z * 1.2, 3));
  const handleZoomOut = () => setZoom(z => Math.max(z / 1.2, 0.3));
  const handleReset = () => {
    setZoom(1);
    setOffset({ x: 0, y: 0 });
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96 bg-gray-900 rounded-lg">
        <RefreshCw className="w-6 h-6 animate-spin text-gray-400" />
        <span className="ml-2 text-gray-400">Building graph...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-96 bg-red-900/20 rounded-lg">
        <span className="text-red-400">{error}</span>
      </div>
    );
  }

  if (nodes.length === 0) {
    return (
      <div className="flex items-center justify-center h-96 bg-gray-900 rounded-lg">
        <div className="text-center">
          <p className="text-gray-400 mb-2">No entities to visualize</p>
          <p className="text-gray-500 text-sm">Store some memories to see the entity graph</p>
        </div>
      </div>
    );
  }

  return (
    <div className="relative">
      {/* Controls */}
      <div className="absolute top-4 right-4 z-10 flex gap-2">
        <button
          onClick={handleZoomIn}
          className="p-2 bg-gray-800 rounded-lg hover:bg-gray-700 text-white"
          title="Zoom In"
        >
          <ZoomIn className="w-4 h-4" />
        </button>
        <button
          onClick={handleZoomOut}
          className="p-2 bg-gray-800 rounded-lg hover:bg-gray-700 text-white"
          title="Zoom Out"
        >
          <ZoomOut className="w-4 h-4" />
        </button>
        <button
          onClick={handleReset}
          className="p-2 bg-gray-800 rounded-lg hover:bg-gray-700 text-white"
          title="Reset View"
        >
          <Maximize2 className="w-4 h-4" />
        </button>
        <button
          onClick={fetchGraphData}
          className="p-2 bg-gray-800 rounded-lg hover:bg-gray-700 text-white"
          title="Refresh"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {/* Legend */}
      <div className="absolute top-4 left-4 z-10 bg-gray-800/90 rounded-lg p-3 text-xs">
        <div className="font-medium text-white mb-2">Entity Types</div>
        {Object.entries(TYPE_COLORS).filter((_, i) => i % 2 === 0).map(([type, color]) => (
          <div key={type} className="flex items-center gap-2 text-gray-300">
            <div className="w-3 h-3 rounded-full" style={{ backgroundColor: color }} />
            <span className="capitalize">{type}</span>
          </div>
        ))}
        <div className="mt-2 pt-2 border-t border-gray-700 text-gray-400">
          {nodes.length} entities · {edges.length} relationships
        </div>
      </div>

      {/* Hover tooltip */}
      {hoveredNode && (
        <div className="absolute bottom-4 left-4 z-10 bg-gray-800 rounded-lg p-3 text-sm">
          <div className="font-medium text-white">{hoveredNode.name}</div>
          <div className="text-gray-400 capitalize">{hoveredNode.type}</div>
          <div className="text-gray-500">{hoveredNode.memoryCount} memories</div>
        </div>
      )}

      {/* Canvas */}
      <canvas
        ref={canvasRef}
        width={900}
        height={500}
        className="w-full h-[500px] rounded-lg cursor-grab active:cursor-grabbing"
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
      />
    </div>
  );
}
