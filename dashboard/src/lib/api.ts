// API client for Remembra backend

const API_BASE = '/api/v1';

export interface Memory {
  id: string;
  content: string;
  user_id: string;
  project_id: string;
  memory_type?: string;
  entities?: string[];
  created_at: string;
  updated_at?: string;
  accessed_at?: string;
  access_count?: number;
  ttl?: string;
  metadata?: Record<string, unknown>;
  relevance?: number;
}

export interface RecallResult {
  memories: Memory[];
  context: string;
  query: string;
  total: number;
}

export interface ListMemoriesParams {
  limit?: number;
  offset?: number;
  project_id?: string;
}

export interface RecallParams {
  query: string;
  limit?: number;
  threshold?: number;
  project_id?: string;
}

// Decay/Temporal types
export interface MemoryDecayInfo {
  id: string;
  content_preview: string;
  relevance_score: number;
  stability: number;
  days_since_access: number;
  access_count: number;
  should_prune: boolean;
  ttl_remaining_seconds: number | null;
  is_expired: boolean;
}

export interface DecayReportResponse {
  user_id: string;
  project_id: string;
  total_memories: number;
  prune_candidates: number;
  average_relevance: number;
  config: {
    prune_threshold: number;
    base_decay_rate: number;
    newness_grace_days: number;
  };
  memories: MemoryDecayInfo[];
}

export interface CleanupResponse {
  dry_run: boolean;
  expired_found: number;
  expired_deleted: number;
  decayed_found: number;
  decayed_pruned: number;
  decayed_archived: number;
  duration_ms: number;
  errors: string[];
}

class ApiClient {
  private apiKey: string | null = null;
  private userId: string | null = null;
  private projectId: string | null = null;

  setApiKey(key: string) {
    this.apiKey = key;
    localStorage.setItem('remembra_api_key', key);
  }

  getApiKey(): string | null {
    if (!this.apiKey) {
      this.apiKey = localStorage.getItem('remembra_api_key');
    }
    return this.apiKey;
  }

  clearApiKey() {
    this.apiKey = null;
    localStorage.removeItem('remembra_api_key');
  }

  setUserId(userId: string) {
    this.userId = userId;
    localStorage.setItem('remembra_user_id', userId);
  }

  getUserId(): string {
    if (!this.userId) {
      this.userId = localStorage.getItem('remembra_user_id') || 'default_user';
    }
    return this.userId;
  }

  setProjectId(projectId: string) {
    this.projectId = projectId;
    localStorage.setItem('remembra_project_id', projectId);
  }

  getProjectId(): string {
    if (!this.projectId) {
      this.projectId = localStorage.getItem('remembra_project_id') || 'default';
    }
    return this.projectId;
  }

  clearAll() {
    this.apiKey = null;
    this.userId = null;
    this.projectId = null;
    localStorage.removeItem('remembra_api_key');
    localStorage.removeItem('remembra_user_id');
    localStorage.removeItem('remembra_project_id');
  }

  private async fetchApi<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<T> {
    const apiKey = this.getApiKey();
    if (!apiKey) {
      throw new Error('API key not set');
    }

    const response = await fetch(`${API_BASE}${endpoint}`, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': apiKey,
        ...options.headers,
      },
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `API error: ${response.status}`);
    }

    return response.json();
  }

  async listMemories(params: ListMemoriesParams = {}): Promise<Memory[]> {
    // No GET /memories endpoint - use recall with broad query
    const result = await this.fetchApi<{ memories: Memory[]; context: string }>('/memories/recall', {
      method: 'POST',
      body: JSON.stringify({
        query: '*',  // Broad query to get all memories
        limit: params.limit || 20,
        threshold: 0.0,  // Low threshold to include all
        project_id: params.project_id || this.getProjectId(),
        user_id: this.getUserId(),
      }),
    });
    return result.memories || [];
  }

  async recallMemories(params: RecallParams): Promise<RecallResult> {
    const response = await this.fetchApi<{ memories: Memory[]; context: string; entities: unknown[] }>('/memories/recall', {
      method: 'POST',
      body: JSON.stringify({
        query: params.query,
        limit: params.limit || 10,
        threshold: params.threshold || 0.4,
        project_id: params.project_id || this.getProjectId(),
        user_id: this.getUserId(),
      }),
    });
    return {
      memories: response.memories || [],
      context: response.context || '',
      query: params.query,
      total: response.memories?.length || 0,
    };
  }

  async getMemory(id: string): Promise<Memory> {
    return this.fetchApi<Memory>(`/memories/${id}`);
  }

  async deleteMemory(id: string): Promise<void> {
    await this.fetchApi<{ deleted_count: number }>(`/memories?memory_id=${id}`, {
      method: 'DELETE',
    });
  }

  async updateMemory(id: string, content: string): Promise<Memory> {
    return this.fetchApi<Memory>(`/memories/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ content }),
    });
  }

  async storeMemory(content: string, projectId?: string, ttl?: string): Promise<Memory> {
    const body: Record<string, string> = {
      content,
      project_id: projectId || this.getProjectId(),
      user_id: this.getUserId(),
    };
    if (ttl) {
      body.ttl = ttl;
    }
    return this.fetchApi<Memory>('/memories', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  // Temporal/Decay methods
  async getDecayReport(
    projectId: string = 'default',
    limit: number = 50
  ): Promise<DecayReportResponse> {
    return this.fetchApi<DecayReportResponse>(
      `/temporal/decay/report?project_id=${projectId}&limit=${limit}`
    );
  }

  async runCleanup(
    projectId: string = 'default',
    dryRun: boolean = true,
    includeDecayed: boolean = false
  ): Promise<CleanupResponse> {
    return this.fetchApi<CleanupResponse>(
      `/temporal/cleanup?project_id=${projectId}&dry_run=${dryRun}&include_decayed=${includeDecayed}`,
      { method: 'POST' }
    );
  }

  async getMemoryDecay(memoryId: string): Promise<MemoryDecayInfo> {
    return this.fetchApi<MemoryDecayInfo>(`/temporal/memory/${memoryId}/decay`);
  }

  // Entity methods
  async listEntities(
    projectId: string = 'default',
    entityType?: string,
    limit: number = 100
  ): Promise<EntitiesListResponse> {
    let url = `/entities?project_id=${projectId}&limit=${limit}`;
    if (entityType) {
      url += `&entity_type=${entityType}`;
    }
    return this.fetchApi<EntitiesListResponse>(url);
  }

  async getEntity(entityId: string): Promise<EntityResponse> {
    return this.fetchApi<EntityResponse>(`/entities/${entityId}`);
  }

  async getEntityRelationships(entityId: string): Promise<RelationshipsListResponse> {
    return this.fetchApi<RelationshipsListResponse>(`/entities/${entityId}/relationships`);
  }

  async getEntityMemories(entityId: string, limit: number = 20): Promise<EntityMemoriesResponse> {
    return this.fetchApi<EntityMemoriesResponse>(`/entities/${entityId}/memories?limit=${limit}`);
  }

  // Debug / Analytics methods
  async debugRecall(query: string, limit: number = 10, threshold: number = 0.3): Promise<DebugRecallResponse> {
    return this.fetchApi<DebugRecallResponse>('/debug/recall', {
      method: 'POST',
      body: JSON.stringify({
        query,
        limit,
        threshold,
        user_id: this.getUserId(),
        project_id: this.getProjectId(),
      }),
    });
  }

  async getAnalytics(): Promise<AnalyticsResponse> {
    return this.fetchApi<AnalyticsResponse>('/debug/analytics');
  }

  async getEntityGraph(projectId: string = 'default'): Promise<EntityGraphDataResponse> {
    return this.fetchApi<EntityGraphDataResponse>(`/debug/entities/graph?project_id=${projectId}`);
  }

  async getMemoryTimeline(page: number = 1, pageSize: number = 50): Promise<MemoryTimelineResponse> {
    return this.fetchApi<MemoryTimelineResponse>(`/debug/timeline?page=${page}&page_size=${pageSize}`);
  }

  // Cloud / Usage methods
  async getUsage(): Promise<UsageResponse> {
    return this.fetchApi<UsageResponse>('/cloud/usage');
  }

  async getPlanInfo(): Promise<PlanInfoResponse> {
    return this.fetchApi<PlanInfoResponse>('/cloud/plan');
  }
}

// Entity types
export interface EntityResponse {
  id: string;
  canonical_name: string;
  type: string;
  aliases: string[];
  attributes: Record<string, unknown>;
  confidence: number;
  memory_count: number;
}

export interface RelationshipResponse {
  id: string;
  from_entity_id: string;
  from_entity_name: string;
  to_entity_id: string;
  to_entity_name: string;
  type: string;
  confidence: number;
}

export interface EntitiesListResponse {
  entities: EntityResponse[];
  total: number;
  by_type: Record<string, number>;
}

export interface RelationshipsListResponse {
  relationships: RelationshipResponse[];
  total: number;
}

export interface EntityMemoriesResponse {
  entity_id: string;
  entity_name: string;
  memories: Array<{
    id: string;
    content: string;
    created_at: string;
  }>;
  total: number;
}

// Debug / Analytics types
export interface ScoringBreakdown {
  memory_id: string;
  content: string;
  created_at: string | null;
  semantic_score: number;
  keyword_score: number;
  hybrid_score: number;
  rerank_score: number | null;
  recency_score: number;
  entity_score: number;
  access_score: number;
  final_score: number;
  matched_entities: string[];
  matched_keywords: string[];
  age_days: number | null;
}

export interface DebugRecallResponse {
  query: string;
  latency_ms: number;
  config: Record<string, unknown>;
  results: ScoringBreakdown[];
  context_tokens: number;
  context_truncated: number;
  context_dropped: number;
  matched_entities: Array<{ id: string; name: string; type: string; confidence: number }>;
  related_entities: Array<{ id: string; name: string; type: string; confidence: number }>;
  pipeline_stages: string[];
  total_candidates: number;
  filtered_count: number;
}

export interface AnalyticsResponse {
  total_memories: number;
  total_entities: number;
  total_relationships: number;
  entities_by_type: Record<string, number>;
  age_distribution: Record<string, number>;
  avg_decay_score: number;
  healthy_memories: number;
  stale_memories: number;
  critical_memories: number;
  stores_today: number;
  recalls_today: number;
}

export interface EntityGraphNode {
  id: string;
  label: string;
  type: string;
  confidence: number;
  memory_count: number;
}

export interface EntityGraphEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  confidence: number;
}

export interface EntityGraphDataResponse {
  nodes: EntityGraphNode[];
  edges: EntityGraphEdge[];
  stats: Record<string, unknown>;
}

export interface TimelineMemory {
  id: string;
  content: string;
  created_at: string;
  project_id: string;
  access_count: number;
  last_accessed: string | null;
  entities: Array<{ name: string; type: string }>;
}

export interface MemoryTimelineResponse {
  memories: TimelineMemory[];
  total: number;
  page: number;
  page_size: number;
}

export interface UsageResponse {
  user_id: string;
  plan: string;
  period: string;
  stores: number;
  recalls: number;
  deletes: number;
  active_days: number;
  limits: Record<string, number>;
}

export interface PlanInfoResponse {
  plan: string;
  limits: Record<string, unknown>;
  usage: Record<string, number>;
  limit_checks: Record<string, { allowed: boolean; reason?: string; limit?: number; current?: number }>;
}

export const api = new ApiClient();
