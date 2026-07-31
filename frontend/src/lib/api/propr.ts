import { fetchApi } from './core';

// PROPR-1: Propr.xyz prop-firm integration. Every route except /api/propr/status
// 404s while the hidden backend flag (FORVEN_PROPR_ENABLED) is off, and status
// reports only { enabled: false } — the frontend treats any failure as "off".

export interface ProprStatus {
	enabled: boolean;
	allow_live?: boolean;
	api_key_configured?: boolean;
	base_url?: string;
	connected?: boolean;
	connection_error?: string;
	user_id?: string;
	account_id?: string;
	attempt_id?: string;
	attempt_status?: string;
	account_type?: string | null;
	orders_allowed?: boolean;
	closes_allowed?: boolean;
	account_value?: number | null;
	account_error?: string;
}

export interface ProprMirrorCandidate {
	id: string;
	name: string;
	stage: string;
	timeframe?: string | null;
}

export interface ProprMirrorTradeState {
	status: string;
	reason?: string | null;
	strategy?: string;
	asset?: string;
	direction?: string;
	quantity?: number;
	entry_price?: number | null;
	exit_price?: number | null;
	opened_at?: string;
	closed_at?: string;
	recorded_at?: string;
	source_execution_type?: string;
	risk_usd?: number;
	attempts?: number;
	/** RETRY-1: this entry re-armed after a terminal failure while its entry signal stayed live. */
	retry_signal_gated?: boolean;
	retry_rearmed_at?: string;
}

export interface ProprMirrorHalt {
	day?: string;
	day_start_equity?: number;
	equity?: number;
	daily_loss?: number;
	daily_loss_limit_usd?: number;
	daily_halt_at_usd?: number;
	drawdown_used?: number;
	drawdown_allowance_usd?: number;
	drawdown_type?: string;
	starting_balance?: number;
	profit_target_usd?: number | null;
	profit_progress_usd?: number;
	rules_source?: string;
	halted?: boolean;
	reasons?: string[];
	checked_at?: string;
	/** PROPR-ANCHOR-1: how today's day-start equity was determined. */
	anchor_source?: string;
	daily_rule_fully_enforced?: boolean;
}

export interface ProprMirror {
	enabled: boolean;
	strategies: Record<string, string>;
	candidates: ProprMirrorCandidate[];
	state: Record<string, ProprMirrorTradeState>;
	halt?: ProprMirrorHalt;
	/** Orphaned venue positions the mirror found but does not own. */
	unmanaged?: Record<string, unknown>;
	/**
	 * SLICE-1: the challenge account is divided equally across the roster and each
	 * member sizes within its own share. Before this, every member sized off the
	 * FULL balance — six of them could put 2x the account at risk and exhaust the
	 * drawdown allowance in a single session.
	 */
	capital_slice?: {
		roster_size?: number | null;
		challenge_equity_usd?: number | null;
		slice_usd?: number | null;
		reason?: string | null;
		/** MIRROR-RISK-1: the RESOLVED Settings-page risk, whole percent. */
		risk_pct?: number | null;
		risk_usd_per_trade?: number | null;
	} | null;
}

export interface ProprOverview {
	status: ProprStatus;
	positions: Record<string, unknown>[] | null;
	orders: Record<string, unknown>[] | null;
	trades: Record<string, unknown>[] | null;
	attempts: Record<string, unknown>[] | null;
	challenges: Record<string, unknown>[] | null;
	errors?: Record<string, string>;
}

export interface ProprConnectionCheck {
	name: string;
	ok: boolean;
	detail: unknown;
}

export async function getProprEnabled(): Promise<boolean> {
	// The only propr call the sidebar makes; remote=false keeps it instant
	// (no upstream Propr API round-trip just to render the nav).
	try {
		const res = await fetchApi<ProprStatus>('/api/propr/status?remote=false');
		return Boolean(res?.enabled);
	} catch {
		return false;
	}
}

export async function getProprStatus(remote = true): Promise<ProprStatus> {
	return fetchApi(`/api/propr/status?remote=${remote}`);
}

export async function getProprOverview(): Promise<ProprOverview> {
	return fetchApi('/api/propr/overview');
}

export async function setProprApiKey(apiKey: string): Promise<{ ok: boolean; status: ProprStatus }> {
	return fetchApi('/api/propr/api-key', {
		method: 'PUT',
		body: JSON.stringify({ api_key: apiKey })
	});
}

export async function clearProprApiKey(): Promise<{ ok: boolean }> {
	return fetchApi('/api/propr/api-key', { method: 'DELETE' });
}

export async function getProprMirror(): Promise<ProprMirror> {
	return fetchApi('/api/propr/mirror');
}

export async function updateProprMirror(update: {
	enabled?: boolean;
	strategies?: string[];
}): Promise<{ ok: boolean; enabled: boolean; strategies: Record<string, string> }> {
	return fetchApi('/api/propr/mirror', {
		method: 'PUT',
		body: JSON.stringify(update)
	});
}

export async function tickProprMirror(): Promise<{ ok: boolean; result: Record<string, unknown> }> {
	return fetchApi('/api/propr/mirror/tick', { method: 'POST' });
}

export async function closeProprPosition(
	asset: string,
	positionSide: 'long' | 'short',
	quantity: number
): Promise<{ ok: boolean; result: Record<string, unknown> }> {
	return fetchApi('/api/propr/positions/close', {
		method: 'POST',
		body: JSON.stringify({ asset, position_side: positionSide, quantity, confirm: true })
	});
}

export async function cancelProprOrder(
	orderId: string
): Promise<{ ok: boolean; result: Record<string, unknown> }> {
	return fetchApi(`/api/propr/orders/${encodeURIComponent(orderId)}/cancel`, {
		method: 'POST'
	});
}

export async function runProprConnectionTest(): Promise<{
	ok: boolean;
	base_url: string;
	checks: ProprConnectionCheck[];
}> {
	return fetchApi('/api/propr/connection-test', { method: 'POST' });
}
