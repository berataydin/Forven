<script lang="ts">
	/**
	 * Dashboard preview — the decision-first redesign, built alongside the
	 * current dashboard for review (preview-first steer, 2026-07-30). Renders
	 * ENTIRELY from GET /api/dashboard/snapshot: one server-timestamped
	 * payload, per-section truth status, and a derived "needs attention"
	 * inbox. No existing page or panel is touched.
	 *
	 * Tiers: attention → money → machine → research.
	 */
	import { onDestroy, onMount } from 'svelte';
	import {
		createRealtimeRefresh,
		type RealtimeRefreshController,
	} from '$lib/utils/realtime';
	import { refreshSnapshot, snapshotState } from '$lib/stores/dashboardSnapshotStore';
	import SnapshotSection from '$lib/components/dashboard_preview/SnapshotSection.svelte';
	import AttentionInbox from '$lib/components/dashboard_preview/AttentionInbox.svelte';
	import KpisStrip from '$lib/components/dashboard_preview/KpisStrip.svelte';
	import TradingTile from '$lib/components/dashboard_preview/TradingTile.svelte';
	import EquityTile from '$lib/components/dashboard_preview/EquityTile.svelte';
	import PaperTile from '$lib/components/dashboard_preview/PaperTile.svelte';
	import SystemTile from '$lib/components/dashboard_preview/SystemTile.svelte';
	import DataTile from '$lib/components/dashboard_preview/DataTile.svelte';
	import SchedulerTile from '$lib/components/dashboard_preview/SchedulerTile.svelte';
	import AgentsTile from '$lib/components/dashboard_preview/AgentsTile.svelte';
	import PipelineTile from '$lib/components/dashboard_preview/PipelineTile.svelte';
	import LeaderboardTile from '$lib/components/dashboard_preview/LeaderboardTile.svelte';
	import { fmtAge } from '$lib/components/dashboard_preview/format';

	// Client poll is cheap by contract: the endpoint serves a cached payload
	// and never runs a data source read.
	const POLL_MS = 10_000;
	// After this many consecutive failed polls the page-level OFFLINE state
	// engages (single miss = transient; data stays visible either way).
	const OFFLINE_AFTER_FAILURES = 2;

	let realtime: RealtimeRefreshController | null = null;
	let clock: ReturnType<typeof setInterval> | null = null;
	let now = Date.now();

	$: state = $snapshotState;
	$: snapshot = state.snapshot;
	$: sections = snapshot?.sections ?? {};
	$: inboxItems = snapshot?.inbox?.data?.items ?? [];
	$: clientOffline = state.consecutiveFailures >= OFFLINE_AFTER_FAILURES;
	$: offlineForText = state.failedSince ? fmtAge(new Date(state.failedSince).toISOString(), now) : null;

	onMount(() => {
		void refreshSnapshot();
		realtime = createRealtimeRefresh(refreshSnapshot, {
			fallbackMs: POLL_MS,
			wsDebounceMs: 2000,
			wsEvents: [
				'kill_switch_activated',
				'kill_switch_cleared',
				'risk_alert',
				'approval_created',
				'approval_resolved',
				'strategy_promoted',
				'task_failed',
			],
			pollWhenWsOfflineOnly: false,
		});
		realtime.start();
		clock = setInterval(() => {
			now = Date.now();
		}, 1000);
	});

	onDestroy(() => {
		realtime?.stop();
		if (clock) clearInterval(clock);
	});
</script>

<svelte:head>
	<title>Dashboard Preview | Forven</title>
	<meta
		name="description"
		content="Preview of the decision-first operations dashboard: one system-truth snapshot, needs-attention inbox, and truthful staleness everywhere."
	/>
</svelte:head>

<div class="flex h-full min-h-0 flex-col gap-3 overflow-hidden bg-black px-4 py-6">
	<div class="flex-shrink-0 border-b border-[#222] pb-3">
		<div class="flex items-center justify-between gap-3">
			<div>
				<h1 class="text-lg font-bold uppercase tracking-widest text-white">
					Dashboard <span class="text-amber-400">Preview</span>
				</h1>
				<p class="mt-1 text-xs text-[#666]">
					Decision-first redesign candidate — one snapshot, explicit staleness, needs-attention inbox. The
					current <a href="/" class="underline hover:text-white">dashboard</a> is unchanged.
				</p>
			</div>
			<div class="text-right font-mono text-[10px] uppercase tracking-wider">
				{#if clientOffline}
					<div class="border border-red-800 bg-red-500/10 px-2 py-1 text-red-400" data-testid="page-offline">
						Backend unreachable{offlineForText ? ` for ${offlineForText}` : ''} — showing last snapshot
					</div>
				{:else if snapshot?.generated_at}
					<div class="text-[#555]" data-testid="page-generated">
						snapshot generated {fmtAge(snapshot.generated_at, now)} ago
					</div>
				{/if}
			</div>
		</div>
	</div>

	{#if !snapshot}
		<div class="text-xs text-gray-500" data-testid="page-loading">
			{clientOffline ? 'Backend unreachable — no snapshot received yet.' : 'Loading snapshot…'}
		</div>
	{:else}
		<div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
			<div class="space-y-3 pb-3">
				<KpisStrip data={sections.kpis?.data ?? null} />

				<!-- Tier 1: what needs me right now -->
				<section>
					<h2 class="mb-1 text-[10px] font-bold uppercase tracking-widest text-gray-400">Needs attention now</h2>
					{#if snapshot.inbox?.status === 'unavailable'}
						<div class="border border-[#333] bg-[#0a0a0a] px-3 py-2 text-xs text-gray-500" data-testid="inbox-unavailable">
							Attention inbox has no data yet — unknown, not "all clear".
						</div>
					{:else}
						<AttentionInbox items={inboxItems} {now} />
					{/if}
				</section>

				<!-- Tier 2: what is the money doing -->
				<section>
					<h2 class="mb-1 text-[10px] font-bold uppercase tracking-widest text-gray-400">Money</h2>
					<div class="grid grid-cols-1 gap-2 lg:grid-cols-3">
						<SnapshotSection title="Trading" section={sections.trading} {now} {clientOffline} href="/trading" testid="preview-trading" let:data>
							<TradingTile {data} />
						</SnapshotSection>
						<SnapshotSection title="Equity" section={sections.equity} {now} {clientOffline} href="/portfolio" testid="preview-equity" let:data>
							<EquityTile {data} />
						</SnapshotSection>
						<SnapshotSection title="Paper sessions" section={sections.paper} {now} {clientOffline} href="/paper-trades" testid="preview-paper" let:data>
							<PaperTile {data} />
						</SnapshotSection>
					</div>
				</section>

				<!-- Tier 3: is the machine alive -->
				<section>
					<h2 class="mb-1 text-[10px] font-bold uppercase tracking-widest text-gray-400">Machine</h2>
					<div class="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-4">
						<SnapshotSection title="System" section={sections.system} {now} {clientOffline} href="/diagnostics" testid="preview-system" let:data>
							<SystemTile {data} />
						</SnapshotSection>
						<SnapshotSection title="Data" section={sections.data} {now} {clientOffline} href="/data" testid="preview-data" let:data>
							<DataTile {data} {now} />
						</SnapshotSection>
						<SnapshotSection title="Scheduler" section={sections.scheduler} {now} {clientOffline} href="/agents" testid="preview-scheduler" let:data>
							<SchedulerTile {data} {now} />
						</SnapshotSection>
						<SnapshotSection title="Agents" section={sections.agents} {now} {clientOffline} href="/agents" testid="preview-agents" let:data>
							<AgentsTile {data} />
						</SnapshotSection>
					</div>
				</section>

				<!-- Tier 4: research funnel -->
				<section>
					<h2 class="mb-1 text-[10px] font-bold uppercase tracking-widest text-gray-400">Research</h2>
					<div class="grid grid-cols-1 gap-2 lg:grid-cols-2">
						<SnapshotSection title="Pipeline" section={sections.pipeline} {now} {clientOffline} href="/pipeline" testid="preview-pipeline" let:data>
							<PipelineTile {data} />
						</SnapshotSection>
						<SnapshotSection title="Leaderboard" section={sections.leaderboard} {now} {clientOffline} href="/all-trades" testid="preview-leaderboard" let:data>
							<LeaderboardTile {data} />
						</SnapshotSection>
					</div>
				</section>
			</div>
		</div>
	{/if}
</div>
