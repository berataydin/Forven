import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
import { mount, tick, unmount } from 'svelte';

import DashboardPage from '../routes/+page.svelte';
import { load } from '../routes/+page';
import type {
	DashboardActivityItem,
	DashboardOverview,
	WinnerEntry,
} from '$lib/api';

const apiMocks = vi.hoisted(() => ({
	getDashboardActivity: vi.fn(),
	getDashboardOverview: vi.fn(),
	getDashboardWinners: vi.fn(),
}));

const realtimeController = vi.hoisted(() => ({
	start: vi.fn(),
	stop: vi.fn(),
}));

const appStoreMocks = vi.hoisted(() => {
	let pageValue = {
		params: {},
		url: new URL('http://localhost/'),
	};
	const subscribers = new Set<(value: typeof pageValue) => void>();

	return {
		page: {
			subscribe(callback: (value: typeof pageValue) => void) {
				callback(pageValue);
				subscribers.add(callback);
				return () => subscribers.delete(callback);
			},
		},
		setUrl(url: string) {
			pageValue = {
				...pageValue,
				url: new URL(url),
			};
			for (const subscriber of subscribers) {
				subscriber(pageValue);
			}
		},
	};
});

vi.mock('$app/stores', () => ({
	page: appStoreMocks.page,
}));

vi.mock('$lib/api', () => apiMocks);
vi.mock('$lib/stores', () => ({
	backendConnected: writable(true),
}));
vi.mock('$lib/utils/realtime', () => ({
	createRealtimeRefresh: vi.fn(() => realtimeController),
}));
vi.mock('$lib/components/dashboard/OpsHeaderStrip.svelte', async () => ({
	default: (await import('./fixtures/StatusStripStub.svelte')).default,
}));
vi.mock('$lib/components/dashboard/SystemPulsePanel.svelte', async () => ({
	default: (await import('./fixtures/Stub.svelte')).default,
}));
vi.mock('$lib/components/dashboard/DataIntegrityPanel.svelte', async () => ({
	default: (await import('./fixtures/Stub.svelte')).default,
}));
vi.mock('$lib/components/dashboard/AlertsFeed.svelte', async () => ({
	default: (await import('./fixtures/Stub.svelte')).default,
}));
vi.mock('$lib/components/dashboard/PaperSessionSummary.svelte', async () => ({
	default: (await import('./fixtures/Stub.svelte')).default,
}));
vi.mock('$lib/components/dashboard/PipelineFlowPanel.svelte', async () => ({
	default: (await import('./fixtures/Stub.svelte')).default,
}));
vi.mock('$lib/components/dashboard/ActivityStream.svelte', async () => ({
	default: (await import('./fixtures/Stub.svelte')).default,
}));
vi.mock('$lib/components/dashboard/StrategyLeaderboard.svelte', async () => ({
	default: (await import('./fixtures/Stub.svelte')).default,
}));
vi.mock('$lib/components/dashboard/EquityOverlay.svelte', async () => ({
	default: (await import('./fixtures/Stub.svelte')).default,
}));
vi.mock('$lib/components/dashboard/LiveTradingPanel.svelte', async () => ({
	default: (await import('./fixtures/Stub.svelte')).default,
}));

type MountedComponent = ReturnType<typeof mount>;

function buildData(): {
	overview: DashboardOverview;
	activity: DashboardActivityItem[];
	winners: WinnerEntry[];
} {
	return {
		overview: {
			kpis: {
				total_tested: 1,
				best_sharpe: 1.2,
				active_scans: 1,
				signals_today: 1,
				pipeline_count: 1,
				data_coverage: 0.5,
			},
			lifecycle_counts: {},
			blocked_count: 0,
			last_ingestion_at: null,
			autopilot: {
				running: false,
				paused: false,
				run_id: null,
				worker_concurrency: 0,
				active_workers: 0,
				queued_jobs: 0,
				dead_letter_jobs: 0,
				last_tick_error: null,
				health_ok: true,
			},
			timestamp: '2026-03-12T00:00:00Z',
		},
		activity: [],
		winners: [],
	};
}

async function flush(): Promise<void> {
	await Promise.resolve();
	await tick();
	await Promise.resolve();
	await tick();
}

describe('dashboard Command Deck layout', () => {
	let app: MountedComponent | null = null;
	let target: HTMLDivElement;

	beforeEach(() => {
		target = document.createElement('div');
		document.body.appendChild(target);

		appStoreMocks.setUrl('http://localhost/');
		apiMocks.getDashboardActivity.mockResolvedValue([]);
		apiMocks.getDashboardOverview.mockResolvedValue(buildData().overview);
		apiMocks.getDashboardWinners.mockResolvedValue([]);
		realtimeController.start.mockClear();
		realtimeController.stop.mockClear();
		try {
			localStorage.clear();
		} catch {
			// jsdom localStorage might be unavailable in rare setups; ignore.
		}
	});

	afterEach(() => {
		if (app) {
			unmount(app);
			app = null;
		}
		target.remove();
		vi.clearAllMocks();
	});

	it('renders Command Deck layout (no tab nav)', async () => {
		app = mount(DashboardPage, {
			target,
			props: { data: buildData() },
		});

		await flush();

		expect(target.querySelector('[data-testid="dashboard-view-link-primary"]')).toBeNull();
		expect(target.querySelector('[data-testid="dashboard-view-link-quant_factory"]')).toBeNull();
		expect(target.querySelector('[data-testid="dashboard-view-link-beta"]')).toBeNull();
		expect(target.querySelector('[data-testid="status-strip"]')).toBeTruthy();
	});

	it('renders empty states when all loader endpoints return null/empty', async () => {
		// Override the beforeEach mocks so onMount's loadDashboard also returns null/empty,
		// simulating the full "all endpoints empty" scenario end-to-end.
		apiMocks.getDashboardOverview.mockResolvedValue(null);
		apiMocks.getDashboardActivity.mockResolvedValue([]);
		apiMocks.getDashboardWinners.mockResolvedValue([]);

		const loaderData = {
			overview: null,
			activity: [],
			winners: [],
		};

		app = mount(DashboardPage, {
			target,
			props: { data: loaderData as any },
		});

		await flush();
		await flush();

		// Page must mount without throwing and show the core shell (ops header stub).
		expect(target.querySelector('[data-testid="status-strip"]')).toBeTruthy();
		// Activity stream toggle exists (default collapsed, just the button).
		expect(target.querySelector('[data-testid="activity-stream-toggle"]')).toBeTruthy();
	});

	it('persists activity stream collapse to localStorage', async () => {
		app = mount(DashboardPage, {
			target,
			props: { data: buildData() },
		});

		await flush();

		const toggle = target.querySelector<HTMLButtonElement>(
			'[data-testid="activity-stream-toggle"]',
		);
		expect(toggle).toBeTruthy();

		// Default: collapsed, key is absent / 'false'.
		const initial = localStorage.getItem('dashboard.activityStream.expanded');
		expect(initial === null || initial === 'false').toBe(true);

		toggle!.click();
		await flush();
		expect(localStorage.getItem('dashboard.activityStream.expanded')).toBe('true');

		toggle!.click();
		await flush();
		expect(localStorage.getItem('dashboard.activityStream.expanded')).toBe('false');
	});
});

describe('legacy tab redirects', () => {
	beforeEach(() => {
		for (const fn of Object.values(apiMocks)) (fn as ReturnType<typeof vi.fn>).mockResolvedValue(null);
		apiMocks.getDashboardActivity.mockResolvedValue([]);
		apiMocks.getDashboardWinners.mockResolvedValue([]);
	});

	it('redirects ?view=quant_factory to /', async () => {
		const url = new URL('http://localhost/?view=quant_factory');
		await expect(load({ url } as any)).rejects.toMatchObject({ status: 301, location: '/' });
	});

	it('redirects ?view=beta to /', async () => {
		const url = new URL('http://localhost/?view=beta');
		await expect(load({ url } as any)).rejects.toMatchObject({ status: 301, location: '/' });
	});

	it('does NOT redirect when no view param present', async () => {
		const url = new URL('http://localhost/');
		const result = await load({ url } as any);
		expect(result).toHaveProperty('overview');
	});
});
