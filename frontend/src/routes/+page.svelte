<script lang="ts">
	/**
	 * The operations dashboard. Renders ENTIRELY from GET /api/dashboard/snapshot:
	 * one server-timestamped payload, per-section truth status, and a derived
	 * "needs attention" inbox. Sections keep last-good data through backend
	 * outages and say so explicitly — unknown never renders as a reassuring zero.
	 *
	 * Tiers: attention → money → machine → research.
	 */
	import { onDestroy, onMount } from 'svelte';
	import {
		createRealtimeRefresh,
		type RealtimeRefreshController,
	} from '$lib/utils/realtime';
	import { refreshSnapshot, snapshotState } from '$lib/stores/dashboardSnapshotStore';
	import SnapshotSection from '$lib/components/dashboard_snapshot/SnapshotSection.svelte';
	import AttentionInbox from '$lib/components/dashboard_snapshot/AttentionInbox.svelte';
	import KpisStrip from '$lib/components/dashboard_snapshot/KpisStrip.svelte';
	import TradingTile from '$lib/components/dashboard_snapshot/TradingTile.svelte';
	import EquityTile from '$lib/components/dashboard_snapshot/EquityTile.svelte';
	import PaperTile from '$lib/components/dashboard_snapshot/PaperTile.svelte';
	import SystemTile from '$lib/components/dashboard_snapshot/SystemTile.svelte';
	import DataTile from '$lib/components/dashboard_snapshot/DataTile.svelte';
	import SchedulerTile from '$lib/components/dashboard_snapshot/SchedulerTile.svelte';
	import AgentsTile from '$lib/components/dashboard_snapshot/AgentsTile.svelte';
	import PipelineTile from '$lib/components/dashboard_snapshot/PipelineTile.svelte';
	import LeaderboardTile from '$lib/components/dashboard_snapshot/LeaderboardTile.svelte';
	import { fmtAge } from '$lib/components/dashboard_snapshot/format';

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
	<title>Operations | Forven</title>
	<meta
		name="description"
		content="Decision-first operations dashboard: one system-truth snapshot, needs-attention inbox, and explicit staleness on every tile."
	/>
</svelte:head>

<div class="flex h-full min-h-0 flex-col gap-3 overflow-hidden bg-black px-4 py-6">
	<div class="flex-shrink-0 border-b border-[#222] pb-3">
		<div class="flex items-center justify-between gap-3">
			<h1 class="text-lg font-bold uppercase tracking-widest text-white">Operations</h1>
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
						<SnapshotSection title="Trading" section={sections.trading} {now} {clientOffline} href="/trading" testid="dash-trading" let:data>
							<TradingTile {data} />
						</SnapshotSection>
						<SnapshotSection title="Equity" section={sections.equity} {now} {clientOffline} href="/portfolio" testid="dash-equity" let:data>
							<EquityTile {data} />
						</SnapshotSection>
						<SnapshotSection title="Paper sessions" section={sections.paper} {now} {clientOffline} href="/paper-trades" testid="dash-paper" let:data>
							<PaperTile {data} />
						</SnapshotSection>
					</div>
				</section>

				<!-- Tier 3: is the machine alive -->
				<section>
					<h2 class="mb-1 text-[10px] font-bold uppercase tracking-widest text-gray-400">Machine</h2>
					<div class="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-4">
						<SnapshotSection title="System" section={sections.system} {now} {clientOffline} href="/diagnostics" testid="dash-system" let:data>
							<SystemTile {data} />
						</SnapshotSection>
						<SnapshotSection title="Data" section={sections.data} {now} {clientOffline} href="/data" testid="dash-data" let:data>
							<DataTile {data} {now} />
						</SnapshotSection>
						<SnapshotSection title="Scheduler" section={sections.scheduler} {now} {clientOffline} href="/agents" testid="dash-scheduler" let:data>
							<SchedulerTile {data} {now} />
						</SnapshotSection>
						<SnapshotSection title="Agents" section={sections.agents} {now} {clientOffline} href="/agents" testid="dash-agents" let:data>
							<AgentsTile {data} />
						</SnapshotSection>
					</div>
				</section>

				<!-- Tier 4: research funnel -->
				<section>
					<h2 class="mb-1 text-[10px] font-bold uppercase tracking-widest text-gray-400">Research</h2>
					<div class="grid grid-cols-1 gap-2 lg:grid-cols-2">
						<SnapshotSection title="Pipeline" section={sections.pipeline} {now} {clientOffline} href="/pipeline" testid="dash-pipeline" let:data>
							<PipelineTile {data} />
						</SnapshotSection>
						<SnapshotSection title="Leaderboard" section={sections.leaderboard} {now} {clientOffline} href="/all-trades" testid="dash-leaderboard" let:data>
							<LeaderboardTile {data} />
						</SnapshotSection>
					</div>
				</section>
			</div>
		</div>
	{/if}
</div>
