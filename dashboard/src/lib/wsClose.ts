/**
 * Close codes sent by the real-time endpoint (src/remembra/api/v1/websocket.py).
 *
 * 1000 is a deliberate close. 4001 means the key or session the socket used no
 * longer works: it was revoked, deleted, signed out or expired, or the account
 * was turned off. Reconnecting with the same credentials would be refused again,
 * so the socket stays closed until the credentials change (a new sign-in).
 */
export const WS_CLOSE_NORMAL = 1000;
export const WS_CLOSE_UNAUTHORIZED = 4001;

/** Whether to reconnect automatically after the socket closed with `code`. */
export function shouldReconnect(code: number): boolean {
  return code !== WS_CLOSE_NORMAL && code !== WS_CLOSE_UNAUTHORIZED;
}
