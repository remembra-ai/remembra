import { ApiKeyManager } from '../components/ApiKeyManager';

/**
 * API Keys page wrapper
 * 
 * This page provides a dedicated view for managing API keys.
 * It can be used as a standalone route or embedded in the Dashboard tabs.
 * 
 * Features:
 * - List all API keys with name, preview, permissions, and usage info
 * - Create new API keys with customizable permissions
 * - Secure key display (shown only once on creation)
 * - Delete/revoke keys with confirmation
 */
export function ApiKeys() {
  return <ApiKeyManager />;
}

export default ApiKeys;
