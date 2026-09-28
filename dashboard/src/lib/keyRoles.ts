/**
 * Roles the dashboard can create an API key with.
 *
 * The server refuses `admin` from a dashboard session (403): admin keys are
 * created only with the server's master key. tests/test_authz_viewer_read_only.py
 * creates a key with every role and checks that this list is exactly the set
 * the server accepts, so the two cannot drift apart.
 */
export const CREATABLE_KEY_ROLES = ['editor', 'viewer'] as const;

export type CreatableKeyRole = (typeof CREATABLE_KEY_ROLES)[number];

/** What each role can do, as the server enforces it (auth/rbac.py ROLE_PERMISSIONS). */
export const KEY_ROLE_SUMMARIES: Record<CreatableKeyRole, string> = {
  editor: 'Store, recall, change and delete memories. Manage webhooks. Create and revoke editor or viewer keys.',
  viewer: 'Read only. Recall memories, read entities and list keys. Cannot store, change or delete anything, except revoke itself.',
};

export const ADMIN_KEY_NOTE = "Admin keys can only be created with the server's master key.";
