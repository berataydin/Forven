<script lang="ts">
	/**
	 * Agent staleness is computed from heartbeat AGES in the snapshot — the
	 * `agent_stalled` websocket event is never emitted by the backend, and a
	 * log-tail view makes a hung agent look identical to a working one.
	 */
	import { asList, asRecord, fmtAgeSeconds } from './format';

	export let data: Record<string, unknown> | null;

	$: record = asRecord(data);
	$: roster = asList(record.roster);

	function beatClass(agent: Record<string, unknown>): string {
		const active = typeof agent.active_tasks === 'number' ? agent.active_tasks : 0;
		const age = agent.last_activity_age_seconds;
		if (active === 0) return 'text-gray-500';
		if (typeof age !== 'number') return 'text-gray-500';
		if (age > 3600) return 'text-red-400';
		if (age > 1800) return 'text-amber-400';
		return 'text-emerald-400';
	}
</script>

<div class="font-mono text-xs" data-testid="agents-tile">
	{#if roster.length === 0}
		<div class="text-gray-600">No agents registered.</div>
	{:else}
		<div class="space-y-0.5">
			{#each roster as agent}
				<div class="flex items-center justify-between gap-2 text-[11px]">
					<span class="flex min-w-0 items-center gap-1.5">
						<span
							class={`h-1.5 w-1.5 shrink-0 rounded-full ${
								typeof agent.active_tasks === 'number' && agent.active_tasks > 0 ? 'bg-emerald-400' : 'bg-[#333]'
							}`}
						></span>
						<span class="truncate text-gray-400" title={String(agent.name ?? agent.id ?? '')}>{agent.name ?? agent.id}</span>
					</span>
					<span class="shrink-0 text-gray-500">
						{String(agent.active_tasks ?? '—')} run · {String(agent.pending_tasks ?? '—')} queued
						<span class={beatClass(agent)} title="last activity">
							· {typeof agent.last_activity_age_seconds === 'number'
								? fmtAgeSeconds(agent.last_activity_age_seconds)
								: '—'}</span
						>
					</span>
				</div>
			{/each}
		</div>
	{/if}
</div>
