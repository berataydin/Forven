/**
 * Diagnostics API client (T11) — backs the /diagnostics page (T12) and
 * the LaunchBanner (T15).
 *
 * Contract is fixed by ``forven/diagnostics.py::snapshot()`` and
 * ``forven/routers/diagnostics.py``. If the backend payload shape
 * changes, update the interfaces here in lockstep.
 */

import { fetchApi } from './core';
import type { MainnetArmingFields, MainnetArmingState } from './types';

export type { MainnetArmingState };

export type CheckStatus = 'pass' | 'warn' | 'fail';

export interface CheckResult {
	name: string;
	status: CheckStatus;
	summary: string;
	detail: Record<string, unknown>;
	/** ISO-8601 UTC timestamp of when this individual check ran. */
	checked_at?: string | null;
}

export interface DiagnosticsSummary {
	pass: number;
	warn: number;
	fail: number;
}

export interface McpServerRow {
	name: string;
	transport: string | null;
	enabled: boolean;
	last_status: string | null;
	last_status_at: string | null;
	last_error_short: string | null;
}

export interface DiagnosticsSnapshot extends MainnetArmingFields {
	generated_at: string;
	overall: CheckStatus;
	summary: DiagnosticsSummary;
	checks: CheckResult[];
	mcp_servers: McpServerRow[];
}

/**
 * OPS-4: is this backend armed to spend REAL money?
 *
 * `mainnet_armed` is deliberately NOT defaulted to `false` when absent: an older
 * backend that does not report it is UNKNOWN, and rendering unknown as "not
 * armed" is the failure mode the flag exists to prevent. Callers get `null` and
 * must show that as unknown.
 */
export function readMainnetArmed(snapshot: MainnetArmingFields | null | undefined): boolean | null {
	if (!snapshot || typeof snapshot.mainnet_armed !== 'boolean') return null;
	return snapshot.mainnet_armed;
}

export interface CheckpointSnapshot {
	key: string;
	payload: unknown;
	created_at: string;
	updated_at: string;
}

export interface ResumableTask {
	id: number;
	display_id: string | null;
	agent_id: string | null;
	/** Task type (e.g. 'general', 'backtest', 'trade_execution'). */
	type: string | null;
	title: string;
	started_at: string | null;
	interrupted_at: string | null;
	latest_checkpoint: CheckpointSnapshot | null;
	checkpoint_count: number;
}

export interface ResumableTasksResponse {
	tasks: ResumableTask[];
}

export interface ResumeTaskResponse {
	ok: boolean;
	task_id: number;
}

export async function getDiagnosticsSnapshot(): Promise<DiagnosticsSnapshot> {
	return fetchApi<DiagnosticsSnapshot>('/diagnostics/snapshot');
}

export async function getResumableTasks(): Promise<ResumableTasksResponse> {
	return fetchApi<ResumableTasksResponse>('/diagnostics/resumable');
}

export async function resumeTask(taskId: number): Promise<ResumeTaskResponse> {
	return fetchApi<ResumeTaskResponse>(`/diagnostics/resumable/${taskId}/resume`, {
		method: 'POST',
	});
}
