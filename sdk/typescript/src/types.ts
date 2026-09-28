/**
 * Remembra TypeScript SDK Types
 */

// ============================================================================
// Configuration
// ============================================================================

export interface RemembraConfig {
  /** Remembra server URL */
  url?: string;
  /** API key for authentication */
  apiKey?: string;
  /** User ID for memory operations */
  userId: string;
  /** Project namespace */
  project?: string;
  /** Request timeout in milliseconds */
  timeout?: number;
  /** Enable debug logging */
  debug?: boolean;
}

// ============================================================================
// Core Types
// ============================================================================

export interface EntityRef {
  id: string;
  canonical_name: string;
  type: string;
  confidence: number;
}

export interface Memory {
  id: string;
  content: string;
  relevance: number;
  created_at: string;
  metadata?: Record<string, unknown>;
}

// ============================================================================
// Store
// ============================================================================

export interface StoreOptions {
  /** Optional key-value metadata */
  metadata?: Record<string, unknown>;
  /**
   * Time-to-live: a number and a unit, e.g. "30d", "36h", "1y". Units: s, min, h, d, w,
   * mo (30 days), y (365 days). Servers after 0.16.1 refuse (422) a TTL they cannot read, and a bare `m`
   * (write `min` or `mo`); 0.16.1 and earlier ignore an unreadable TTL and read `m` as months.
   */
  ttl?: string;
}

export interface StoreResult {
  id: string;
  extracted_facts: string[];
  entities: EntityRef[];
}

// ============================================================================
// Recall
// ============================================================================

export interface RecallOptions {
  /** Maximum results (1-50) */
  limit?: number;
  /** Minimum relevance threshold (0.0-1.0) */
  threshold?: number;
  /** Maximum tokens in context */
  maxTokens?: number;
  /** Enable hybrid search */
  enableHybrid?: boolean;
  /** Enable reranking */
  enableRerank?: boolean;
}

export interface RecallResult {
  context: string;
  memories: Memory[];
  entities: EntityRef[];
}

// ============================================================================
// Forget
// ============================================================================

/** Give exactly one of `memoryId`, `entity` or `allMemories: true`. */
export interface ForgetOptions {
  /** Delete this one memory */
  memoryId?: string;
  /** Delete the memories linked to the entity with this exact name or alias (any case) */
  entity?: string;
  /** With `entity` only: limit the delete to this project */
  projectId?: string;
  /** Delete every memory, entity and relationship in the account. Never implied. */
  allMemories?: boolean;
}

export interface ForgetResult {
  deleted_memories: number;
  deleted_entities: number;
  deleted_relationships: number;
}

// ============================================================================
// Conversation Ingestion
// ============================================================================

export interface Message {
  /** Message role: user, assistant, or system */
  role: 'user' | 'assistant' | 'system';
  /** Message content */
  content: string;
  /** Optional speaker name */
  name?: string;
  /** Optional timestamp (ISO format) */
  timestamp?: string;
  /** Optional metadata */
  metadata?: Record<string, unknown>;
}

export interface IngestOptions {
  /** Session ID for grouping conversations */
  sessionId?: string;
  /** Which messages to extract from */
  extractFrom?: 'user' | 'assistant' | 'both';
  /** Minimum importance threshold (0.0-1.0) */
  minImportance?: number;
  /** Enable deduplication */
  dedupe?: boolean;
  /** Store results (false for dry-run) */
  store?: boolean;
  /** Enable extraction (false to store raw) */
  infer?: boolean;
}

export interface ExtractedFact {
  content: string;
  confidence: number;
  importance: number;
  source_message_index: number;
  speaker: string | null;
  stored: boolean;
  memory_id: string | null;
  action: 'add' | 'update' | 'delete' | 'noop' | 'skipped';
  action_reason: string | null;
}

export interface ExtractedEntity {
  name: string;
  type: string;
  relationship: string | null;
}

export interface IngestStats {
  messages_processed: number;
  facts_extracted: number;
  facts_stored: number;
  facts_updated: number;
  facts_deduped: number;
  facts_skipped: number;
  entities_found: number;
  processing_time_ms: number;
}

export interface IngestResult {
  status: 'ok' | 'partial' | 'error';
  session_id: string | null;
  facts: ExtractedFact[];
  entities: ExtractedEntity[];
  stats: IngestStats;
}

// ============================================================================
// Errors
// ============================================================================

export interface RemembraErrorDetails {
  status: number;
  message: string;
  code?: string;
}
