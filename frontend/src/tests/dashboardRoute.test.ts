import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { mount, unmount } from 'svelte';

const apiMock = vi.hoisted(() => ({
	getDashboardSnapshot: vi.fn(),
}));
const realtimeController = vi.hoisted(() => ({ start: vi.fn(), stop: vi.fn() }));

vi.mock('$lib/api/snapshot', () => apiMock);
vi.mock('$lib/utils/realtime', () => ({
	createRealtimeRefresh: vi.fn(() => realtimeController),
}));

import { fmtAgeSeconds, fmtNum, fmtUsd } from '../lib/components/dashboard_snapshot/format';
import SnapshotSection from '../lib/components/dashboard_snapshot/SnapshotSection.svelte';
import AttentionInbox from '../lib/components/dashboard_snapshot/AttentionInbox.svelte';
import { refreshSnapshot, snapshotState } from '../lib/stores/dashboardSnapshotStore';
import DashboardPage from '../routes/+page.svelte';
import { load } from '../routes/+page';

let target: HTMLElement;
let instance: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (instance) {
		unmount(instance);
		instance = null;
	}
	target?.remove();
	apiMock.getDashboardSnapshot.mockReset();
	snapshotState.set({
		snapshot: null,
		lastGoodFetchAt: null,
		failedSince: null,
		consecutiveFailures: 0,
	});
});

async function flush(): Promise<void> {
	await Promise.resolve();
	await new Promise((resolve) => setTimeout(resolve, 0));
	await Promise.resolve();
}

function mountComponent(component: unknown, props: Record<string, unknown>): void {
	target = document.createElement('div');
	document.body.appendChild(target);
	instance = mount(component as never, { target, props });
}

function section(status: string, data: Record<string, unknown> | null, asOfSecondsAgo = 5) {
	return {
		status,
		as_of: data === null && status === 'unavailable' ? null : new Date(Date.now() - asOfSecondsAgo * 1000).toISOString(),
		last_attempt_at: new Date().toISOString(),
		error_code: status === 'error' ? 'TimeoutError' : status === 'unavailable' ? 'ValueError' : null,
		data,
	};
}

function buildSnapshot(): Record<string, unknown> {
	const policies: Record<string, unknown> = {};
	for (const name of [
		'system',
		'trading',
		'paper',
		'data',
		'scheduler',
		'agents',
		'pipeline',
		'approvals',
		'equity',
		'leaderboard',
		'kpis',
	]) {
		policies[name] = { refresh_seconds: 10, stale_after_seconds: 60 };
	}
	return {
		contract_version: 1,
		generated_at: new Date().toISOString(),
		served_at: new Date().toISOString(),
		policies,
		sections: {
			system: section('fresh', { status: 'ok', worker_loops: [], queues: {} }),
			trading: section('fresh', {
				execution_mode: 'paper',
				risk: { kill_switch_active: true, daily_loss_halt: false, drawdown_pct: 0.01 },
				daily_risk: { start_equity: 1000, current_equity: 990 },
				account: {},
			}),
			paper: section('fresh', { totals: { session_count: 2, open_count: 1, realized_pnl_usd: 12.5, win_rate_pct: 50 }, sessions: [] }),
			data: section('stale', { dataset_count: 4, last_ingestion_at: new Date().toISOString(), quality_avg_score: 95, orphan_count: 0 }, 120),
			scheduler: section('fresh', { jobs: [], overdue_job_ids: [], error_job_ids: [] }),
			agents: section('fresh', { roster: [] }),
			pipeline: section('fresh', {
				stages: [{ state: 'paper', count: 3 }],
				needs_attention: null,
				needs_attention_unavailable_reason: 'pipeline_explain_not_merged',
				recent_events: [],
			}),
			approvals: section('fresh', { pending_count: 1, items: [] }),
			equity: section('error', { base: 1000, curve: [1000, 1010, 990] }, 300),
			leaderboard: section('fresh', { entries: [], winners: [] }),
			kpis: section('fresh', {
				kpis: {
					total_tested: 7,
					best_sharpe: null,
					active_scans: 0,
					signals_today: null,
					pipeline_count: 3,
					data_coverage: null,
				},
				unknown_fields: { signals_today: 'no_signal_counter_source' },
				autopilot: { running: true, dead_letter_jobs: 0 },
			}),
		},
		inbox: section('fresh', {
			items: [
				{
					id: 'halt:kill_switch',
					severity: 'critical',
					source: 'trading',
					message: 'Kill switch is active — live entries are blocked.',
					action_label: 'Open Risk',
					action_href: '/risk',
					entity_id: null,
					first_observed_at: new Date(Date.now() - 60_000).toISOString(),
					last_observed_at: new Date().toISOString(),
				},
			],
		}),
	};
}

describe('format truth rules', () => {
	it('renders unknown as an em dash, never zero', () => {
		expect(fmtNum(null)).toBe('—');
		expect(fmtNum(undefined)).toBe('—');
		expect(fmtUsd(null)).toBe('—');
		expect(fmtAgeSeconds(null)).toBe('—');
	});

	it('renders a real zero as zero', () => {
		expect(fmtNum(0)).toBe('0');
		expect(fmtUsd(0)).toBe('$0.00');
	});
});

describe('SnapshotSection wrapper', () => {
	it('fresh section shows a subtle age stamp', () => {
		mountComponent(SnapshotSection, {
			title: 'System',
			section: section('fresh', { ok: 1 }),
			now: Date.now(),
			testid: 'wrap',
		});
		expect(target.querySelector('[data-testid="wrap-chip-fresh"]')).not.toBeNull();
		expect(target.querySelector('[data-testid="wrap-nodata"]')).toBeNull();
	});

	it('stale section shows the STALE chip with age', () => {
		mountComponent(SnapshotSection, {
			title: 'System',
			section: section('stale', { ok: 1 }, 120),
			now: Date.now(),
			testid: 'wrap',
		});
		const chip = target.querySelector('[data-testid="wrap-chip-stale"]');
		expect(chip?.textContent).toContain('STALE');
	});

	it('error section shows FETCH FAILED while keeping last-good data visible', () => {
		mountComponent(SnapshotSection, {
			title: 'System',
			section: section('error', { ok: 1 }, 300),
			now: Date.now(),
			testid: 'wrap',
		});
		const chip = target.querySelector('[data-testid="wrap-chip-error"]');
		expect(chip?.textContent).toContain('FETCH FAILED');
		// Last-good data is still rendered (slot present, no "no data" body).
		expect(target.querySelector('[data-testid="wrap-nodata"]')).toBeNull();
	});

	it('unavailable section says so explicitly — unknown never looks like off', () => {
		mountComponent(SnapshotSection, {
			title: 'System',
			section: section('unavailable', null),
			now: Date.now(),
			testid: 'wrap',
		});
		expect(target.querySelector('[data-testid="wrap-chip-unavailable"]')?.textContent).toContain('UNAVAILABLE');
		expect(target.querySelector('[data-testid="wrap-nodata"]')?.textContent).toContain('unknown, not zero');
	});

	it('client offline overrides everything with the OFFLINE chip', () => {
		mountComponent(SnapshotSection, {
			title: 'System',
			section: section('fresh', { ok: 1 }),
			now: Date.now(),
			clientOffline: true,
			testid: 'wrap',
		});
		expect(target.querySelector('[data-testid="wrap-chip-offline"]')?.textContent).toContain('OFFLINE');
	});
});

describe('AttentionInbox', () => {
	it('renders the calm empty state when nothing needs attention', () => {
		mountComponent(AttentionInbox, { items: [], now: Date.now() });
		expect(target.querySelector('[data-testid="attention-inbox-empty"]')).not.toBeNull();
	});

	it('renders items with severity chip and action link', () => {
		const snapshot = buildSnapshot() as { inbox: { data: { items: unknown[] } } };
		mountComponent(AttentionInbox, { items: snapshot.inbox.data.items, now: Date.now() });
		const item = target.querySelector('[data-testid="attention-item"]');
		expect(item?.textContent).toContain('Kill switch is active');
		const action = target.querySelector('[data-testid="attention-item-action"]') as HTMLAnchorElement;
		expect(action).not.toBeNull();
		expect(action.getAttribute('href')).toBe('/risk');
	});
});

describe('snapshot store last-good retention', () => {
	it('keeps the last good snapshot through fetch failures and stamps failedSince', async () => {
		const payload = buildSnapshot();
		apiMock.getDashboardSnapshot.mockResolvedValueOnce(payload);
		await refreshSnapshot();
		expect(get(snapshotState).snapshot).not.toBeNull();

		apiMock.getDashboardSnapshot.mockRejectedValueOnce(new Error('stall'));
		await refreshSnapshot();
		const failed = get(snapshotState);
		expect(failed.snapshot).not.toBeNull();
		expect(failed.consecutiveFailures).toBe(1);
		expect(failed.failedSince).not.toBeNull();

		apiMock.getDashboardSnapshot.mockResolvedValueOnce(payload);
		await refreshSnapshot();
		const recovered = get(snapshotState);
		expect(recovered.consecutiveFailures).toBe(0);
		expect(recovered.failedSince).toBeNull();
	});
});

describe('dashboard page', () => {
	it('renders tiers from one snapshot with truthful chips and null-safe KPIs', async () => {
		apiMock.getDashboardSnapshot.mockResolvedValue(buildSnapshot());
		mountComponent(DashboardPage, {});
		await flush();

		// KPI strip: unknown renders as —, not 0.
		expect(target.querySelector('[data-testid="kpi-signals-today"]')?.textContent?.trim()).toBe('—');
		// Inbox item with action.
		expect(target.querySelector('[data-testid="attention-item"]')?.textContent).toContain('Kill switch');
		// Money tier: halts are loud.
		expect(target.querySelector('[data-testid="trading-halts"]')?.textContent).toContain('KILL SWITCH');
		// Stale + error chips propagate to their sections.
		expect(target.querySelector('[data-testid="dash-data-chip-stale"]')).not.toBeNull();
		expect(target.querySelector('[data-testid="dash-equity-chip-error"]')).not.toBeNull();
		// Pipeline explain gap is explicit, not silently green.
		expect(target.querySelector('[data-testid="pipeline-tile"]')?.textContent).toContain('unavailable');
	});
});

describe('legacy tab redirects', () => {
	it('redirects ?view=quant_factory to /', () => {
		const url = new URL('http://localhost/?view=quant_factory');
		let thrown: unknown = null;
		try {
			load({ url } as never);
		} catch (error) {
			thrown = error;
		}
		expect(thrown).toMatchObject({ status: 301, location: '/' });
	});

	it('redirects ?view=beta to /', () => {
		const url = new URL('http://localhost/?view=beta');
		let thrown: unknown = null;
		try {
			load({ url } as never);
		} catch (error) {
			thrown = error;
		}
		expect(thrown).toMatchObject({ status: 301, location: '/' });
	});

	it('does NOT redirect when no view param present', () => {
		const url = new URL('http://localhost/');
		expect(load({ url } as never)).toBeUndefined();
	});
});
