import { describe, expect, it } from 'vitest';
import {
  DELETION_COPY,
  REFUND_BEFORE_DELETE,
  deletionConfirm,
  isDeletionCode,
  rememberDeletionNotice,
  takeDeletionNotice,
  teamOwnerRefusal,
} from '../accountDeletion';
import { ApiError } from '../api';

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

describe('after deletion and team owners', () => {
  it('keeps the server message for the sign-in screen, once', () => {
    const store = new Map<string, string>();
    const fake = {
      setItem: (k: string, v: string) => void store.set(k, v),
      getItem: (k: string) => store.get(k) ?? null,
      removeItem: (k: string) => void store.delete(k),
    };
    rememberDeletionNotice('Your account is deleted. All your data is erased permanently after 2026-10-03.', fake);
    expect(takeDeletionNotice(fake)).toContain('2026-10-03');
    expect(takeDeletionNotice(fake)).toBeNull();
    const broken = {
      setItem: () => {
        throw new Error('blocked');
      },
      getItem: () => {
        throw new Error('blocked');
      },
      removeItem: () => undefined,
    };
    expect(() => rememberDeletionNotice('x', broken)).not.toThrow();
    expect(takeDeletionNotice(broken)).toBeNull();
  });

  it('reads the teams from a TEAM_OWNER refusal and ignores other errors', () => {
    const err = new ApiError('You own Crew A (1 other member).', 409, 'TEAM_OWNER', {
      code: 'TEAM_OWNER',
      teams: [{ id: 't1', name: 'Crew A', members: 1 }],
    });
    expect(teamOwnerRefusal(err)).toEqual({ message: 'You own Crew A (1 other member).', teams: [{ id: 't1', name: 'Crew A', members: 1 }] });
    expect(teamOwnerRefusal(new ApiError('Password is incorrect', 400))).toBeNull();
    expect(teamOwnerRefusal(null)).toBeNull();
  });

  it('tells the user to ask for a refund before deleting', () => {
    expect(REFUND_BEFORE_DELETE).toContain('14 days');
    expect(DELETION_COPY).toContain('a cancelled subscription and revoked API keys do not');
  });
});
