// The zone and policy endpoints the Zone Map and the Policy panel use that the
// shared crew client (lib/crew/api.ts, WP-12) has no wrapper for. Built on its
// low-level `request`, so auth, idempotency keys and CrewApiError parsing are
// the same as every other crew call.

import type { CrewApi } from '../../../lib/crew/api';
import type { ClaimView, CrewEvent } from '../../../lib/crew/types';
import type { ZoneChange, ZoneDetail, ZonesListing } from './zoneModel';

export interface BypassCodeRow {
  id: string;
  session_id: string | null;
  scope: string;
  issued_by: string;
  expires_at: string;
  used_at: string | null;
  created_at: string;
  state: 'active' | 'used' | 'expired';
}

/** Editable zone fields (`PATCH /zones/{id}`); `null` clears an optional field. */
export interface ZonePatch {
  title?: string;
  description?: string | null;
  include?: string[];
  exclude?: string[];
  mode?: 'exclusive' | 'shared' | 'watch';
  auto_claim?: boolean;
  protected?: boolean;
  fail_closed?: boolean;
  reserve_for?: string | null;
}

/** `PATCH /zones/{id}`: a server zone applies; a repo zone returns the patch to commit instead (D9). */
export type ZonePatchResult =
  | { applied: true; zone: ZoneDetail }
  | { applied: false; export_patch: string; yaml: string };

const enc = encodeURIComponent;

async function data<T>(p: Promise<{ data: T | null }>): Promise<T> {
  const res = await p;
  if (res.data === null) throw new Error('Empty response');
  return res.data;
}

export function zonesApi(api: CrewApi) {
  const r = api.request;
  return {
    listZones: (crewId: string) => data(r<ZonesListing>(`/crews/${enc(crewId)}/zones`)),
    zoneChanges: (crewId: string, state?: 'pending' | 'applied' | 'rejected') =>
      data(r<{ changes: ZoneChange[] }>(`/crews/${enc(crewId)}/zone-changes`, { query: { state } })).then((b) => b.changes),
    exportZones: (crewId: string) => data(r<{ yaml: string; zones: number }>(`/crews/${enc(crewId)}/zones/export`)),
    patchZone: (zoneId: string, patch: ZonePatch, version: number) =>
      data(r<ZonePatchResult>(`/zones/${enc(zoneId)}`, { method: 'PATCH', body: patch, ifMatch: version })),
    /** (H for server zones) Archive a zone; a repo zone returns the patch that removes it instead. */
    archiveZone: (zoneId: string) =>
      data(r<{ applied: boolean; zone?: ZoneDetail; export_patch?: string; yaml?: string }>(`/zones/${enc(zoneId)}`, { method: 'DELETE' })),
    /** Deterministic zone suggestions from the stored folder tree (no writes). */
    suggestZones: (crewId: string) =>
      data(
        r<{ zones: { slug: string; title: string; include: string[] }[]; yaml: string; reason?: string }>(`/crews/${enc(crewId)}/zones/suggest`, {
          method: 'POST',
          body: {},
        }),
      ),
    /** A human claim on a zone (dashboard source): the first half of "Grant to…". */
    humanClaim: (crewId: string, zoneId: string, reason: string) =>
      data(
        r<{ status: string; claim: ClaimView | null; blockers?: unknown[] }>(`/crews/${enc(crewId)}/claims`, {
          method: 'POST',
          body: { zone_id: zoneId, mode: 'exclusive', wait: false, source: 'dashboard', reason },
        }),
      ),
    releaseClaim: (claimId: string, note: string) =>
      data(r<Record<string, unknown>>(`/claims/${enc(claimId)}/release`, { method: 'POST', body: { note } })),
    /** (H) Bypass codes with their state; never the code itself. */
    bypassCodes: (crewId: string) => data(r<{ codes: BypassCodeRow[]; server_time: string }>(`/crews/${enc(crewId)}/bypass-codes`)),
    /** The event log from `sinceSeq` (≤200 per page). */
    eventsPage: (crewId: string, sinceSeq: number, limit = 200) =>
      data(
        r<{ events: CrewEvent[]; last_seq: number; has_more: boolean }>(`/crews/${enc(crewId)}/events`, {
          query: { since_seq: sinceSeq, limit },
        }),
      ),
  };
}

export type ZonesApi = ReturnType<typeof zonesApi>;
