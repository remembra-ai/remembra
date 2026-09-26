import { describe, expect, it } from 'vitest';
import privacyHtml from '../../../../landing/privacy.html?raw';
import termsHtml from '../../../../landing/terms.html?raw';
import {
  DELETE_ACCOUNT_BILLING,
  DELETE_ACCOUNT_EFFECT,
  DELETE_ACCOUNT_SOCIAL,
  DELETE_ACCOUNT_SUMMARY,
  ERASURE_EMAIL,
} from '../accountDeletion';

const text = (html: string) => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');

describe('delete account copy', () => {
  const all = [DELETE_ACCOUNT_SUMMARY, DELETE_ACCOUNT_EFFECT, DELETE_ACCOUNT_BILLING, DELETE_ACCOUNT_SOCIAL].join(' ');

  it('promises no permanent deletion: the backend deactivates and revokes keys', () => {
    expect(all).not.toMatch(/permanent|cannot be undone|all memories/i);
    expect(DELETE_ACCOUNT_EFFECT).toContain('deactivates it and revokes its API keys');
    expect(DELETE_ACCOUNT_SUMMARY).toContain('revoke its API keys');
  });

  it('says what the privacy policy and terms say about erasure', () => {
    const privacy = text(privacyHtml);
    const terms = text(termsHtml);
    expect(privacy).toContain('deleting it from the dashboard today deactivates it and revokes its keys');
    expect(privacy).toContain('does not yet erase what is stored');
    expect(privacy).toContain(`${ERASURE_EMAIL} from the account's address and we will erase it within 30 days`);
    expect(terms).toContain('Deleting your account from the dashboard deactivates it and revokes its keys');
    for (const copy of [DELETE_ACCOUNT_SUMMARY, DELETE_ACCOUNT_EFFECT]) {
      expect(copy).toContain(ERASURE_EMAIL);
      expect(copy).toContain("from the account's address");
      expect(copy).toContain('within 30 days');
    }
  });

  it('tells a GitHub or Google account how to get a password first', () => {
    expect(DELETE_ACCOUNT_SOCIAL).toContain('GitHub or Google');
    expect(DELETE_ACCOUNT_SOCIAL).toContain('Forgot password');
  });

  it('says a paid plan is cancelled, not left billing', () => {
    expect(DELETE_ACCOUNT_BILLING).toContain('cancelled when you delete');
    expect(DELETE_ACCOUNT_BILLING).toContain('not charged again');
  });
});
