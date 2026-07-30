<script lang="ts">
	import { asList, asRecord, fmtAgeSeconds } from './format';

	export let data: Record<string, unknown> | null;

	$: record = asRecord(data);
	$: loops = asList(record.worker_loops ?? record.workerLoops);
	$: queues = asRecord(record.queues);
	$: issues = Array.isArray(record.issues) ? record.issues.map(String) : [];
	$: overall = typeof record.status === 'string' ? record.status : null;

	function loopAge(loop: Record<string, unknown>): string {
		const age = loop.age_seconds ?? loop.ageSeconds;
		return typeof age === 'number' ? fmtAgeSeconds(age) : '—';
	}
</script>

<div class="font-mono text-xs" data-testid="system-tile">
	<div class="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1">
		<div class="text-gray-500">Overall</div>
		<div
			class={overall === 'ok' || overall === 'green'
				? 'text-emerald-400'
				: overall == null
					? 'text-gray-500'
					: 'text-amber-400'}
		>
			{overall ?? '—'}
		</div>

		<div class="text-gray-500">Queues</div>
		<div class="text-gray-300">
			{String(queues.agent_pending ?? '—')} pending · {String(queues.agent_running ?? '—')} running
			{#if typeof queues.agent_stale_running === 'number' && queues.agent_stale_running > 0}
				<span class="text-amber-400">· {queues.agent_stale_running} stalled</span>
			{/if}
		</div>
	</div>

	{#if loops.length > 0}
		<div class="mt-2 space-y-0.5">
			{#each loops as loop}
				<div class="flex items-center justify-between text-[11px]">
					<span class="flex items-center gap-1.5 text-gray-400">
						<span class={`h-1.5 w-1.5 rounded-full ${loop.fresh === false ? 'bg-red-500' : 'bg-emerald-400'}`}></span>
						{loop.name ?? 'loop'}
					</span>
					<span class={loop.fresh === false ? 'text-red-400' : 'text-gray-500'}>{loopAge(loop)}</span>
				</div>
			{/each}
		</div>
	{/if}

	{#if issues.length > 0}
		<div class="mt-2 space-y-0.5">
			{#each issues.slice(0, 3) as issue}
				<div class="truncate text-[11px] text-amber-300" title={issue}>⚠ {issue}</div>
			{/each}
		</div>
	{/if}
</div>
