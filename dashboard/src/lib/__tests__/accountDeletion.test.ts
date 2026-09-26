import { describe, expect, it } from 'vitest';
import { DELETION_COPY, deletionConfirm, isDeletionCode } from '../accountDeletion';

describe('deletionConfirm', () => {
  it('needs DELETE typed exactly', () => {
    expect(deletionConfirm('password', 'delete', 'pw', '')).toBeNull();
    expect(deletionConfirm('password', 'DELETE', 'pw', '')).toEqual({ password: 'pw' });
  });

  it('needs the password in password mode', () => {
    expect(deletionConfirm('password', 'DELETE', '', '123456')).toBeNull();
  });

  it('needs a six-digit code in code mode and trims it', () => {
    expect(deletionConfirm('code', 'DELETE', 'pw', '12345')).toBeNull();
    expect(deletionConfirm('code', 'DELETE', '', ' 123456 ')).toEqual({ code: '123456' });
    expect(isDeletionCode('12a456')).toBe(false);
  });
});

describe('DELETION_COPY', () => {
  it('says billing stops, when data goes, and how backups age out', () => {
    expect(DELETION_COPY).toContain('cancels any subscription at once');
    expect(DELETION_COPY).toContain('7 days later everything the account holds is erased');
    expect(DELETION_COPY).toContain('24 hours of history');
    expect(DELETION_COPY).toContain('3 newer deploys');
  });
});
