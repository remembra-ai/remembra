// What "Delete account" really does, worded once. The backend deactivates the
// account and revokes its keys (auth/users.py delete_account); it does not
// erase stored data, which is done on request within 30 days, as the privacy
// policy, terms and security page say. A paid plan is cancelled first, at the
// end of the period already paid for (api/v1/billing.py
// end_subscription_before_account_deletion). A vitest keeps this in step with
// landing/privacy.html.

export const ERASURE_EMAIL = 'admin@dolphytech.com';

/** The card on the Settings page. */
export const DELETE_ACCOUNT_SUMMARY =
  'Deactivate your account and revoke its API keys. Stored data is not erased automatically: ' +
  `email ${ERASURE_EMAIL} from the account's address and we erase it within 30 days.`;

/** The first paragraph of the confirmation dialog. */
export const DELETE_ACCOUNT_EFFECT =
  'Deleting your account deactivates it and revokes its API keys: you can no longer sign in, and your agents lose access. ' +
  'It does not erase what is stored yet. To have everything erased, email ' +
  `${ERASURE_EMAIL} from the account's address and we erase it within 30 days.`;

/** Billing: the account cannot reach Billing after this, so the plan is cancelled here. */
export const DELETE_ACCOUNT_BILLING =
  'A paid plan is cancelled when you delete: it is not charged again and ends with the period you have paid for. ' +
  'If it cannot be cancelled, the account is not deleted and you are told why.';

/** Accounts created with GitHub or Google have no password until one is set. */
export const DELETE_ACCOUNT_SOCIAL =
  'Signed up with GitHub or Google? Your account has no password yet: sign out, use "Forgot password" on the sign-in page ' +
  'to set one, then come back here.';
