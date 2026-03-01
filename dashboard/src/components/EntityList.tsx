import { useState, useEffect } from 'react';
import { api } from '../lib/api';
import type { EntityResponse, RelationshipResponse } from '../lib/api';
import { User, Building2, MapPin, Lightbulb, RefreshCw, Link2, ChevronRight, FileText } from 'lucide-react';
import clsx from 'clsx';

interface EntityListProps {
  projectId?: string;
}

const ENTITY_ICONS: Record<string, React.ReactNode> = {
  person: <User className="w-4 h-4" />,
  organization: <Building2 className="w-4 h-4" />,
  company: <Building2 className="w-4 h-4" />,
  location: <MapPin className="w-4 h-4" />,
  place: <MapPin className="w-4 h-4" />,
  concept: <Lightbulb className="w-4 h-4" />,
};

const ENTITY_COLORS: Record<string, string> = {
  person: 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border-blue-200 dark:border-blue-800',
  organization: 'bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300 border-purple-200 dark:border-purple-800',
  company: 'bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300 border-purple-200 dark:border-purple-800',
  location: 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-300 border-green-200 dark:border-green-800',
  place: 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-300 border-green-200 dark:border-green-800',
  concept: 'bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-300 border-yellow-200 dark:border-yellow-800',
};

export function EntityList({ projectId = 'default' }: EntityListProps) {
  const [entities, setEntities] = useState<EntityResponse[]>([]);
  const [byType, setByType] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedEntity, setSelectedEntity] = useState<EntityResponse | null>(null);
  const [relationships, setRelationships] = useState<RelationshipResponse[]>([]);
  const [loadingRelationships, setLoadingRelationships] = useState(false);

  const fetchEntities = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listEntities(projectId, undefined, 200);
      setEntities(response.entities);
      setByType(response.by_type);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load entities');
    } finally {
      setLoading(false);
    }
  };

  const fetchRelationships = async (entityId: string) => {
    setLoadingRelationships(true);
    try {
      const response = await api.getEntityRelationships(entityId);
      setRelationships(response.relationships);
    } catch (err) {
      console.error('Failed to load relationships:', err);
      setRelationships([]);
    } finally {
      setLoadingRelationships(false);
    }
  };

  useEffect(() => {
    fetchEntities();
  }, [projectId]);

  useEffect(() => {
    if (selectedEntity) {
      fetchRelationships(selectedEntity.id);
    } else {
      setRelationships([]);
    }
  }, [selectedEntity]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <RefreshCw className="w-6 h-6 animate-spin text-gray-400" />
        <span className="ml-2 text-gray-500">Loading entities...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-lg bg-red-50 dark:bg-red-900/20 p-4">
        <span className="text-red-700 dark:text-red-300">{error}</span>
        <button
          onClick={fetchEntities}
          className="ml-4 text-red-600 hover:text-red-800 underline"
        >
          Retry
        </button>
      </div>
    );
  }

  if (entities.length === 0) {
    return (
      <div className="text-center py-12">
        <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-gray-100 dark:bg-gray-800 mb-4">
          <User className="w-8 h-8 text-gray-400" />
        </div>
        <h3 className="text-lg font-medium text-gray-900 dark:text-white mb-2">
          No Entities Found
        </h3>
        <p className="text-gray-500 dark:text-gray-400 max-w-sm mx-auto">
          Entities (people, places, organizations) are automatically extracted when you store memories.
        </p>
      </div>
    );
  }

  // Group entities by type
  const groupedEntities = entities.reduce((acc, entity) => {
    const type = entity.type.toLowerCase();
    if (!acc[type]) acc[type] = [];
    acc[type].push(entity);
    return acc;
  }, {} as Record<string, EntityResponse[]>);

  return (
    <div className="space-y-6">
      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatBox
          icon={<User className="w-5 h-5" />}
          label="People"
          value={byType.person || 0}
          color="blue"
        />
        <StatBox
          icon={<Building2 className="w-5 h-5" />}
          label="Organizations"
          value={(byType.organization || 0) + (byType.company || 0)}
          color="purple"
        />
        <StatBox
          icon={<MapPin className="w-5 h-5" />}
          label="Places"
          value={(byType.location || 0) + (byType.place || 0)}
          color="green"
        />
        <StatBox
          icon={<Lightbulb className="w-5 h-5" />}
          label="Concepts"
          value={byType.concept || 0}
          color="yellow"
        />
      </div>

      {/* Refresh button */}
      <div className="flex justify-end">
        <button
          onClick={fetchEntities}
          className="flex items-center gap-2 px-3 py-2 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white"
        >
          <RefreshCw className="w-4 h-4" />
          Refresh
        </button>
      </div>

      {/* Entity Groups */}
      {Object.entries(groupedEntities).map(([type, typeEntities]) => (
        <div key={type} className="space-y-3">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 uppercase tracking-wider flex items-center gap-2">
            {ENTITY_ICONS[type] || <Lightbulb className="w-4 h-4" />}
            {type}s ({typeEntities.length})
          </h3>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {typeEntities.map((entity) => (
              <EntityCard
                key={entity.id}
                entity={entity}
                onClick={() => setSelectedEntity(entity)}
                isSelected={selectedEntity?.id === entity.id}
              />
            ))}
          </div>
        </div>
      ))}

      {/* Selected Entity Detail Modal */}
      {selectedEntity && (
        <EntityDetail
          entity={selectedEntity}
          relationships={relationships}
          loadingRelationships={loadingRelationships}
          onClose={() => setSelectedEntity(null)}
        />
      )}
    </div>
  );
}

interface StatBoxProps {
  icon: React.ReactNode;
  label: string;
  value: number;
  color: 'blue' | 'purple' | 'green' | 'yellow' | 'gray';
}

function StatBox({ icon, label, value, color }: StatBoxProps) {
  const colors = {
    blue: 'bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400',
    purple: 'bg-purple-50 dark:bg-purple-900/20 text-purple-600 dark:text-purple-400',
    green: 'bg-green-50 dark:bg-green-900/20 text-green-600 dark:text-green-400',
    yellow: 'bg-yellow-50 dark:bg-yellow-900/20 text-yellow-600 dark:text-yellow-400',
    gray: 'bg-gray-50 dark:bg-gray-800 text-gray-600 dark:text-gray-400',
  };

  return (
    <div className={clsx('rounded-lg p-4', colors[color])}>
      <div className="flex items-center gap-2 mb-1">
        {icon}
        <span className="text-sm font-medium opacity-80">{label}</span>
      </div>
      <div className="text-2xl font-bold">{value}</div>
    </div>
  );
}

interface EntityCardProps {
  entity: EntityResponse;
  onClick: () => void;
  isSelected: boolean;
}

function EntityCard({ entity, onClick, isSelected }: EntityCardProps) {
  const icon = ENTITY_ICONS[entity.type.toLowerCase()] || <Lightbulb className="w-4 h-4" />;
  const colorClass = ENTITY_COLORS[entity.type.toLowerCase()] || ENTITY_COLORS.concept;

  return (
    <button
      onClick={onClick}
      className={clsx(
        'w-full p-4 rounded-lg border text-left transition-all',
        colorClass,
        isSelected && 'ring-2 ring-blue-500',
        'hover:shadow-md'
      )}
    >
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          {icon}
          <span className="font-medium">{entity.canonical_name}</span>
        </div>
        <ChevronRight className="w-4 h-4 opacity-50" />
      </div>
      <div className="mt-2 flex items-center gap-3 text-xs opacity-70">
        {entity.aliases.length > 0 && (
          <span>
            aka: {entity.aliases.slice(0, 2).join(', ')}
            {entity.aliases.length > 2 && ` +${entity.aliases.length - 2}`}
          </span>
        )}
        <span className="flex items-center gap-1">
          <FileText className="w-3 h-3" />
          {entity.memory_count} memories
        </span>
      </div>
    </button>
  );
}

interface EntityDetailProps {
  entity: EntityResponse;
  relationships: RelationshipResponse[];
  loadingRelationships: boolean;
  onClose: () => void;
}

function EntityDetail({ entity, relationships, loadingRelationships, onClose }: EntityDetailProps) {
  const [memories, setMemories] = useState<Array<{ id: string; content: string; created_at: string }>>([]);
  const [loadingMemories, setLoadingMemories] = useState(false);

  useEffect(() => {
    const fetchMemories = async () => {
      setLoadingMemories(true);
      try {
        const response = await api.getEntityMemories(entity.id, 10);
        setMemories(response.memories);
      } catch (err) {
        console.error('Failed to load memories:', err);
      } finally {
        setLoadingMemories(false);
      }
    };
    fetchMemories();
  }, [entity.id]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
      <div className="w-full max-w-lg bg-white dark:bg-gray-800 rounded-xl shadow-2xl max-h-[80vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className="p-6 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              {ENTITY_ICONS[entity.type.toLowerCase()] || <Lightbulb className="w-6 h-6" />}
              <div>
                <h2 className="text-xl font-bold text-gray-900 dark:text-white">
                  {entity.canonical_name}
                </h2>
                <span className="text-sm text-gray-500 dark:text-gray-400 capitalize">
                  {entity.type}
                </span>
              </div>
            </div>
            <button
              onClick={onClose}
              className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500"
            >
              ×
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {/* Aliases */}
          {entity.aliases.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-2">
                Also known as
              </h3>
              <div className="flex flex-wrap gap-2">
                {entity.aliases.map((alias, i) => (
                  <span
                    key={i}
                    className="px-2 py-1 bg-gray-100 dark:bg-gray-700 rounded text-sm"
                  >
                    {alias}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Relationships */}
          <div>
            <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-2 flex items-center gap-2">
              <Link2 className="w-4 h-4" />
              Relationships
            </h3>
            {loadingRelationships ? (
              <div className="text-sm text-gray-400">Loading...</div>
            ) : relationships.length === 0 ? (
              <div className="text-sm text-gray-400">No relationships found</div>
            ) : (
              <div className="space-y-2">
                {relationships.map((rel) => (
                  <div
                    key={rel.id}
                    className="flex items-center gap-2 text-sm p-2 bg-gray-50 dark:bg-gray-700/50 rounded"
                  >
                    <span className="font-medium">
                      {rel.from_entity_id === entity.id ? entity.canonical_name : rel.from_entity_name}
                    </span>
                    <span className="text-gray-500 dark:text-gray-400">→</span>
                    <span className="text-blue-600 dark:text-blue-400">{rel.type}</span>
                    <span className="text-gray-500 dark:text-gray-400">→</span>
                    <span className="font-medium">
                      {rel.to_entity_id === entity.id ? entity.canonical_name : rel.to_entity_name}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Related Memories */}
          <div>
            <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-2 flex items-center gap-2">
              <FileText className="w-4 h-4" />
              Related Memories ({entity.memory_count})
            </h3>
            {loadingMemories ? (
              <div className="text-sm text-gray-400">Loading...</div>
            ) : memories.length === 0 ? (
              <div className="text-sm text-gray-400">No memories found</div>
            ) : (
              <div className="space-y-2">
                {memories.map((mem) => (
                  <div
                    key={mem.id}
                    className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded text-sm"
                  >
                    <p className="text-gray-700 dark:text-gray-300">{mem.content}</p>
                    <p className="text-xs text-gray-400 mt-1">
                      {new Date(mem.created_at).toLocaleDateString()}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
