// API client for Remembra backend

// Use environment variable for API URL, fallback to relative path for dev proxy
const API_BASE = import.meta.env.VITE_API_URL
  ? `${import.meta.env.VITE_API_URL}/api/v1`
  : '/api/v1';

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
  private jwtToken: string | null = null;
  private userId: string | null = null;
  private projectId: string | null = null;

  // JWT Token methods
  setJwtToken(token: string) {
    this.jwtToken = token;
    localStorage.setItem('remembra_jwt_token', token);
  }

  getJwtToken(): string | null {
    if (!this.jwtToken) {
      this.jwtToken = localStorage.getItem('remembra_jwt_token');
    }
    return this.jwtToken;
  }

  clearJwtToken() {
    this.jwtToken = null;
    localStorage.removeItem('remembra_jwt_token');
  }

  // API Key methods
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
      // Try dedicated user_id first, then extract from user object
      this.userId = localStorage.getItem('remembra_user_id');
      if (!this.userId) {
        const userJson = localStorage.getItem('remembra_user');
        if (userJson) {
          try {
            const user = JSON.parse(userJson);
            this.userId = user.id;
          } catch {
            // Invalid JSON, ignore
          }
        }
      }
      this.userId = this.userId || 'default_user';
    }
    return this.userId;
  }

  setProjectId(projectId: string) {
    this.projectId = projectId;
    localStorage.setItem('remembra_project_id', projectId);
  }

  getProjectId(): string | null {
    if (!this.projectId) {
      this.projectId = localStorage.getItem('remembra_project_id') || null;
    }
    return this.projectId;
  }

  clearAll() {
    this.apiKey = null;
    this.jwtToken = null;
    this.userId = null;
    this.projectId = null;
    localStorage.removeItem('remembra_api_key');
    localStorage.removeItem('remembra_jwt_token');
    localStorage.removeItem('remembra_user_id');
    localStorage.removeItem('remembra_project_id');
    localStorage.removeItem('remembra_user');
  }

  private getAuthHeaders(): Record<string, string> {
    // Prefer JWT token over API key
    const jwtToken = this.getJwtToken();
    if (jwtToken) {
      return { 'Authorization': `Bearer ${jwtToken}` };
    }
    
    const apiKey = this.getApiKey();
    if (apiKey) {
      return { 'X-API-Key': apiKey };
    }
    
    return {};
  }

  isAuthenticated(): boolean {
    return !!this.getJwtToken() || !!this.getApiKey();
  }

  private async fetchApi<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<T> {
    const authHeaders = this.getAuthHeaders();
    if (Object.keys(authHeaders).length === 0) {
      throw new Error('Not authenticated');
    }

    const response = await fetch(`${API_BASE}${endpoint}`, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders,
        ...options.headers,
      },
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      // Handle both string errors and Pydantic validation arrays
      let message = `API error: ${response.status}`;
      if (typeof error.detail === 'string') {
        message = error.detail;
      } else if (Array.isArray(error.detail) && error.detail.length > 0) {
        // Pydantic validation errors are arrays of objects with 'msg' field
        message = error.detail.map((e: { msg?: string }) => e.msg || 'Validation error').join(', ');
      } else if (error.message) {
        message = error.message;
      }
      throw new Error(message);
    }

    return response.json();
  }

  async listMemories(params: ListMemoriesParams = {}): Promise<Memory[]> {
    // No GET /memories endpoint - use recall with broad query
    const result = await this.fetchApi<{ memories: Memory[]; context: string }>('/memories/recall', {
      method: 'POST',
      body: JSON.stringify({
        query: 'all memories',  // Broad semantic query
        limit: params.limit || 20,
        threshold: 0.0,  // Low threshold to include all
        project_id: params.project_id || this.getProjectId() || 'default',
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
        project_id: params.project_id || this.getProjectId() || 'default',
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

  async storeMemory(
    content: string, 
    projectId?: string, 
    ttl?: string,
    visibility?: 'personal' | 'project' | 'team',
    spaceId?: string,
    teamId?: string,
  ): Promise<Memory> {
    const body: Record<string, string | undefined> = {
      content,
      project_id: projectId || this.getProjectId() || 'default',
      user_id: this.getUserId(),
    };
    if (ttl) {
      body.ttl = ttl;
    }
    if (visibility) {
      body.visibility = visibility;
    }
    if (spaceId) {
      body.space_id = spaceId;
    }
    if (teamId) {
      body.team_id = teamId;
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
        project_id: this.getProjectId() || 'default',
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

  async getBillingContext(): Promise<BillingContextResponse> {
    return this.fetchApi<BillingContextResponse>('/cloud/context');
  }

  async getDailyUsage(days: number = 30): Promise<DailyUsageResponse> {
    return this.fetchApi<DailyUsageResponse>(`/cloud/usage/daily?days=${days}`);
  }

  async createCheckout(plan: string): Promise<CheckoutResponse> {
    return this.fetchApi<CheckoutResponse>('/cloud/checkout', {
      method: 'POST',
      body: JSON.stringify({ plan }),
    });
  }

  async createPortalSession(): Promise<PortalResponse> {
    return this.fetchApi<PortalResponse>('/cloud/portal', {
      method: 'POST',
    });
  }

  // API Key Management methods
  async listKeys(): Promise<ApiKeyListResponse> {
    return this.fetchApi<ApiKeyListResponse>('/keys');
  }

  async createKey(name: string, permission: 'admin' | 'editor' | 'viewer'): Promise<CreateApiKeyResponse> {
    return this.fetchApi<CreateApiKeyResponse>('/keys', {
      method: 'POST',
      body: JSON.stringify({ name, permission }),
    });
  }

  async revokeKey(id: string): Promise<RevokeApiKeyResponse> {
    return this.fetchApi<RevokeApiKeyResponse>(`/keys/${id}`, {
      method: 'DELETE',
    });
  }

  // User Profile / Settings methods
  async updateProfile(name: string | null): Promise<UserResponse> {
    return this.fetchApi<UserResponse>('/auth/me', {
      method: 'PATCH',
      body: JSON.stringify({ name }),
    });
  }

  async changePassword(currentPassword: string, newPassword: string): Promise<void> {
    await this.fetchApi<{ message: string }>('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    });
  }

  async deleteAccount(password: string): Promise<void> {
    await this.fetchApi<{ message: string }>('/auth/me', {
      method: 'DELETE',
      body: JSON.stringify({ password }),
    });
  }
}

// User Response type (for settings)
export interface UserResponse {
  id: string;
  email: string;
  name: string | null;
  email_verified: boolean;
  is_active: boolean;
  created_at: string;
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

export interface CheckoutResponse {
  checkout_url: string;
}

export interface PortalResponse {
  portal_url: string;
}

export interface DailyUsageItem {
  date: string;
  stores: number;
  recalls: number;
  deletes: number;
}

export interface DailyUsageResponse {
  user_id: string;
  days: DailyUsageItem[];
}

// Billing Context (determines personal vs team view)
export interface BillingContextResponse {
  context: 'personal' | 'team';
  // Personal context
  plan?: string;
  // Team context
  team_id?: string;
  team_name?: string;
  team_plan?: string;
  role?: string;
  can_manage_billing: boolean;
  owner_email?: string;
  // Common
  limits: Record<string, unknown>;
  usage: Record<string, number>;
}

// API Key Management types
export interface ApiKeyInfo {
  id: string;
  name: string;
  key_preview: string;
  permission: 'admin' | 'editor' | 'viewer';
  created_at: string;
  last_used_at: string | null;
}

export interface ApiKeyListResponse {
  keys: ApiKeyInfo[];
  total: number;
}

export interface CreateApiKeyRequest {
  name: string;
  permission: 'admin' | 'editor' | 'viewer';
}

export interface CreateApiKeyResponse {
  id: string;
  name: string;
  key: string; // Full key - shown only once!
  permission: 'admin' | 'editor' | 'viewer';
  created_at: string;
}

export interface RevokeApiKeyResponse {
  id: string;
  revoked: boolean;
}

export const api = new ApiClient();
