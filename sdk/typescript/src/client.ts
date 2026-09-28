/**
 * Remembra TypeScript Client
 * 
 * Main interface for the Remembra AI Memory Layer.
 * 
 * @example
 * ```typescript
 * import { Remembra } from 'remembra';
 * 
 * const memory = new Remembra({
 *   url: 'http://localhost:8787',
 *   apiKey: 'your-api-key',
 *   userId: 'user_123',
 * });
 * 
 * // Store a memory
 * const stored = await memory.store('User prefers dark mode');
 * 
 * // Recall memories
 * const result = await memory.recall('What are user preferences?');
 * console.log(result.context);
 * 
 * // Ingest a conversation
 * const ingestResult = await memory.ingestConversation([
 *   { role: 'user', content: 'My name is John' },
 *   { role: 'assistant', content: 'Nice to meet you, John!' },
 * ]);
 * ```
 */

import type {
  RemembraConfig,
  StoreOptions,
  StoreResult,
  RecallOptions,
  RecallResult,
  ForgetOptions,
  ForgetResult,
  Message,
  IngestOptions,
  IngestResult,
  Memory,
  EntityRef,
} from './types';

import {
  RemembraError,
  AuthenticationError,
  NotFoundError,
  ValidationError,
  RateLimitError,
  ServerError,
  NetworkError,
  TimeoutError,
} from './errors';

const DEFAULT_URL = 'http://localhost:8787';
// Servers before 0.16.1 deleted the whole account for DELETE /api/v1/memories?entity=...
// The last element is 1 for a release: a pre-release of 0.16.1 comes before the fix.
const ENTITY_DELETE_MIN_SERVER = [0, 16, 1, 1];
// What follows the number in a pre-release or dev build: 0.16.1rc1, 0.16.1.dev3, 0.16.1-beta.2.
const PRE_RELEASE = /^[-.]?(?:a|alpha|b|beta|c|rc|pre|preview|dev)/i;

/** [major, minor, patch, 1 for a release or 0 for a pre-release or dev build], or null. */
function serverVersion(version: unknown): number[] | null {
  const match = /^\s*v?(\d+)\.(\d+)(?:\.(\d+))?(\S*)/.exec(String(version ?? ''));
  if (!match) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3] ?? 0), PRE_RELEASE.test(match[4]) ? 0 : 1];
}

function olderThan(version: number[], minimum: number[]): boolean {
  for (let i = 0; i < minimum.length; i++) {
    if (version[i] !== minimum[i]) return version[i] < minimum[i];
  }
  return false;
}
const DEFAULT_TIMEOUT = 30000;
const MAX_RETRIES = 3;
const RETRY_DELAY = 1000;

export class Remembra {
  private readonly url: string;
  private readonly apiKey?: string;
  private readonly userId: string;
  private readonly project: string;
  private readonly timeout: number;
  private readonly debug: boolean;

  constructor(config: RemembraConfig) {
    this.url = (config.url || DEFAULT_URL).replace(/\/$/, '');
    this.apiKey = config.apiKey;
    this.userId = config.userId;
    this.project = config.project || 'default';
    this.timeout = config.timeout || DEFAULT_TIMEOUT;
    this.debug = config.debug || false;

    if (!this.userId) {
      throw new ValidationError('userId is required');
    }
  }

  // ===========================================================================
  // Private Methods
  // ===========================================================================

  private log(...args: unknown[]): void {
    if (this.debug) {
      console.log('[Remembra]', ...args);
    }
  }

  private async request<T>(
    method: string,
    path: string,
    options: {
      body?: unknown;
      params?: Record<string, string>;
      retries?: number;
    } = {}
  ): Promise<T> {
    const { body, params, retries = MAX_RETRIES } = options;

    // Build URL with query params
    const url = new URL(`${this.url}${path}`);
    if (params) {
      Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined) {
          url.searchParams.set(key, value);
        }
      });
    }

    // Build headers
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'User-Agent': 'remembra-js/0.1.0',
    };
    if (this.apiKey) {
      headers['X-API-Key'] = this.apiKey;
    }

    this.log(method, path, body ? JSON.stringify(body).slice(0, 100) : '');

    // Execute with retry
    let lastError: Error | undefined;
    
    for (let attempt = 0; attempt < retries; attempt++) {
      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), this.timeout);

        const response = await fetch(url.toString(), {
          method,
          headers,
          body: body ? JSON.stringify(body) : undefined,
          signal: controller.signal,
        });

        clearTimeout(timeoutId);

        // Handle response
        if (response.ok) {
          return await response.json() as T;
        }

        // Handle errors
        const errorBody = await response.json().catch(() => ({})) as { detail?: string };
        const errorMessage = errorBody.detail || response.statusText;

        switch (response.status) {
          case 401:
            throw new AuthenticationError(errorMessage);
          case 404:
            throw new NotFoundError(errorMessage);
          case 422:
            throw new ValidationError(errorMessage);
          case 429:
            const retryAfter = parseInt(response.headers.get('Retry-After') || '60', 10);
            if (attempt < retries - 1) {
              this.log(`Rate limited, retrying in ${retryAfter}s...`);
              await this.sleep(retryAfter * 1000);
              continue;
            }
            throw new RateLimitError(errorMessage, retryAfter);
          case 500:
          case 502:
          case 503:
          case 504:
            if (attempt < retries - 1) {
              this.log(`Server error (${response.status}), retrying...`);
              await this.sleep(RETRY_DELAY * Math.pow(2, attempt));
              continue;
            }
            throw new ServerError(errorMessage);
          default:
            throw new RemembraError(errorMessage, response.status);
        }
      } catch (error) {
        lastError = error as Error;

        if (error instanceof RemembraError) {
          throw error;
        }

        if (error instanceof Error) {
          if (error.name === 'AbortError') {
            throw new TimeoutError(`Request timed out after ${this.timeout}ms`);
          }
          if (attempt < retries - 1) {
            this.log(`Network error, retrying...`, error.message);
            await this.sleep(RETRY_DELAY * Math.pow(2, attempt));
            continue;
          }
          throw new NetworkError(error.message);
        }
      }
    }

    throw lastError || new NetworkError('Request failed');
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ===========================================================================
  // Core Operations
  // ===========================================================================

  /**
   * Store a new memory.
   * 
   * @param content - Text content to memorize
   * @param options - Optional metadata and TTL
   * @returns Stored memory with extracted facts and entities
   * 
   * @example
   * ```typescript
   * const result = await memory.store('User prefers dark mode', {
   *   metadata: { source: 'settings' },
   *   ttl: '30d',
   * });
   * console.log(result.extracted_facts);
   * ```
   */
  async store(content: string, options: StoreOptions = {}): Promise<StoreResult> {
    return this.request<StoreResult>('POST', '/api/v1/memories', {
      body: {
        user_id: this.userId,
        project_id: this.project,
        content,
        metadata: options.metadata || {},
        ttl: options.ttl,
      },
    });
  }

  /**
   * Recall memories relevant to a query.
   * 
   * @param query - Natural language query
   * @param options - Recall options (limit, threshold, etc.)
   * @returns Context string and matching memories
   * 
   * @example
   * ```typescript
   * const result = await memory.recall('What are user preferences?');
   * console.log(result.context); // Synthesized context
   * console.log(result.memories); // Individual memories
   * ```
   */
  async recall(query: string, options: RecallOptions = {}): Promise<RecallResult> {
    return this.request<RecallResult>('POST', '/api/v1/memories/recall', {
      body: {
        user_id: this.userId,
        project_id: this.project,
        query,
        limit: options.limit || 5,
        threshold: options.threshold || 0.4,
        max_tokens: options.maxTokens,
        enable_hybrid: options.enableHybrid,
        enable_rerank: options.enableRerank,
      },
    });
  }

  /**
   * Get a specific memory by ID.
   * 
   * @param memoryId - Memory ID
   * @returns Memory details
   */
  async get(memoryId: string): Promise<Memory> {
    return this.request<Memory>('GET', `/api/v1/memories/${memoryId}`);
  }

  /**
   * Forget (delete) memories.
   *
   * Give exactly one of `memoryId`, `entity` or `allMemories: true`; nothing
   * is sent otherwise. The server deletes only the authenticated account's
   * memories.
   *
   * - `memoryId`: that one memory.
   * - `entity`: the memories linked to the entity with this exact name or
   *   alias (any case), in every project unless `projectId` is given, then
   *   the entity itself once no memory mentions it.
   * - `allMemories: true`: every memory, entity and relationship in the
   *   account. Only this option deletes everything; it is never implied.
   *
   * @param options - What to delete
   * @returns Deletion counts
   * @throws ValidationError when no target or more than one is given
   * @throws RemembraError (code `SERVER_TOO_OLD`) for `entity` when the server is older than
   *   0.16.1 (a 0.16.1 pre-release or dev build counts as older), or reports no version: those
   *   delete the whole account for it; nothing is sent
   *
   * @example
   * ```typescript
   * // Delete specific memory
   * await memory.forget({ memoryId: 'mem_123' });
   *
   * // Delete the memories about an entity in one project
   * await memory.forget({ entity: 'John', projectId: 'work' });
   * ```
   */
  async forget(options: ForgetOptions): Promise<ForgetResult> {
    const { memoryId, entity, projectId, allMemories } = options ?? {};
    if (entity !== undefined && !entity.trim()) {
      throw new ValidationError('forget({ entity }) needs a non-blank entity name');
    }
    const targets = [memoryId, entity, allMemories === true].filter(Boolean).length;
    if (targets !== 1) {
      throw new ValidationError('forget() needs exactly one of memoryId, entity or allMemories: true');
    }
    if (projectId !== undefined && (!entity || !projectId.trim())) {
      throw new ValidationError('forget({ projectId }) only limits an entity delete and must not be blank');
    }

    const params: Record<string, string> = {};
    if (memoryId) {
      params.memory_id = memoryId;
    } else if (entity) {
      await this.requireSafeEntityDelete();
      params.entity = entity.trim();
      if (projectId !== undefined) {
        params.project_id = projectId.trim();
      }
    } else {
      params.all_memories = 'true';
    }

    return this.request<ForgetResult>('DELETE', '/api/v1/memories', { params });
  }

  /** Refuse a delete by entity when the server would delete the whole account for it (before 0.16.1). */
  private async requireSafeEntityDelete(): Promise<void> {
    const reported = (await this.health()).version;
    const version = serverVersion(reported);
    if (!version || olderThan(version, ENTITY_DELETE_MIN_SERVER)) {
      throw new RemembraError(
        `Not sent: the server reports ${reported || 'no version'}, and a server before 0.16.1 deletes every memory ` +
          'in the account for a delete by entity. Upgrade the server, or delete by memoryId.',
        undefined,
        'SERVER_TOO_OLD',
      );
    }
  }

  // ===========================================================================
  // Conversation Ingestion
  // ===========================================================================

  /**
   * Ingest a conversation and automatically extract memories.
   * 
   * This is the primary method for AI agents to add conversation context
   * to persistent memory without manually calling store for each fact.
   * 
   * @param messages - Array of conversation messages
   * @param options - Ingestion options
   * @returns Extracted facts, entities, and stats
   * 
   * @example
   * ```typescript
   * const result = await memory.ingestConversation([
   *   { role: 'user', content: 'My name is John and I work at Google' },
   *   { role: 'assistant', content: 'Nice to meet you, John!' },
   * ], {
   *   minImportance: 0.5,
   * });
   * 
   * console.log(`Extracted ${result.stats.facts_extracted} facts`);
   * console.log(`Stored ${result.stats.facts_stored} new memories`);
   * ```
   */
  async ingestConversation(
    messages: Message[],
    options: IngestOptions = {}
  ): Promise<IngestResult> {
    return this.request<IngestResult>('POST', '/api/v1/ingest/conversation', {
      body: {
        messages,
        user_id: this.userId,
        project_id: this.project,
        session_id: options.sessionId,
        options: {
          extract_from: options.extractFrom || 'both',
          min_importance: options.minImportance ?? 0.5,
          dedupe: options.dedupe ?? true,
          store: options.store ?? true,
          infer: options.infer ?? true,
        },
      },
    });
  }

  // ===========================================================================
  // Utilities
  // ===========================================================================

  /**
   * Check server health.
   * 
   * @returns Health status
   */
  async health(): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>('GET', '/health');
  }

  /**
   * List entities for the current user.
   * 
   * @returns Array of entities
   */
  async listEntities(): Promise<EntityRef[]> {
    const result = await this.request<{ entities: EntityRef[] }>(
      'GET',
      '/api/v1/entities',
      { params: { user_id: this.userId, project_id: this.project } }
    );
    return result.entities || [];
  }
}
