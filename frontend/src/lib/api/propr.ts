import { fetchApi } from './core';

// PROPR-1: Propr.xyz prop-firm integration. Every route except /api/propr/status
// 404s while the hidden backend flag (FORVEN_PROPR_ENABLED) is off, and status
// reports only { enabled: false } — the frontend treats any failure as "off".

export interface ProprStatus {
	enabled: boolean;
	allow_live?: boolean;
	api_key_configured?: boolean;
	base_url?: string;
	live_venue?: string;
	connected?: boolean;
	connection_error?: string;
	user_id?: string;
	account_id?: string;
	attempt_id?: string;
	attempt_status?: string;
	account_value?: number | null;
	account_error?: string;
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

export async function setProprLiveVenue(
	venue: 'hyperliquid' | 'propr'
): Promise<{ ok: boolean; status: ProprStatus }> {
	return fetchApi('/api/propr/live-venue', {
		method: 'POST',
		body: JSON.stringify({ venue, confirm: true })
	});
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
