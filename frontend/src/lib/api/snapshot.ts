/**
 * Client for GET /api/dashboard/snapshot — the single server-timestamped
 * system-truth payload behind the operations dashboard (contract v1).
 *
 * Truth rules mirrored from the backend contract:
 * - a section's `data` is the last GOOD payload; `status` says how much to
 *   trust it (fresh | stale | error | unavailable)
 * - unknown numerics arrive as null and must render as "—", never 0
 * - freshness thresholds come from `policies`; the UI must not invent its own
 */
import { fetchApi } from './core';

export type SectionStatus = 'fresh' | 'stale' | 'error' | 'unavailable';

export interface SnapshotPolicy {
	refresh_seconds: number;
	stale_after_seconds: number;
}

export interface SnapshotSectionPayload<T = Record<string, unknown>> {
	status: SectionStatus;
	as_of: string | null;
	last_attempt_at: string | null;
	error_code: string | null;
	data: T | null;
}

export interface InboxItem {
	id: string;
	severity: 'critical' | 'warning' | 'info';
	source: string;
	message: string;
	action_label: string | null;
	action_href: string | null;
	entity_id: string | null;
	first_observed_at: string;
	last_observed_at: string;
}

export interface DashboardSnapshot {
	contract_version: number;
	generated_at: string | null;
	served_at: string;
	policies: Record<string, SnapshotPolicy>;
	sections: Record<string, SnapshotSectionPayload>;
	inbox: SnapshotSectionPayload<{ items: InboxItem[] }>;
}

export async function getDashboardSnapshot(): Promise<DashboardSnapshot> {
	return fetchApi<DashboardSnapshot>('/dashboard/snapshot', { timeoutMs: 8000 });
}
