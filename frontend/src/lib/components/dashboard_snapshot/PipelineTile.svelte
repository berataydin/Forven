<script lang="ts">
	import { asList, asRecord, fmtNum } from './format';

	export let data: Record<string, unknown> | null;

	$: record = asRecord(data);
	$: stages = asList(record.stages);
	$: needs = record.needs_attention === null ? null : asList(record.needs_attention);
	$: needsReason =
		typeof record.needs_attention_unavailable_reason === 'string'
			? record.needs_attention_unavailable_reason
			: null;
	$: events = asList(record.recent_events);
</script>

<div class="font-mono text-xs" data-testid="pipeline-tile">
	{#if stages.length > 0}
		<div class="flex flex-wrap gap-1.5">
			{#each stages as stage}
				<div class="border border-[#222] bg-[#0a0a0a] px-2 py-1 text-center">
					<div class="text-sm text-gray-200">{fmtNum(stage.count)}</div>
					<div class="text-[9px] uppercase tracking-wider text-gray-500">{stage.state ?? '—'}</div>
				</div>
			{/each}
		</div>
	{:else}
		<div class="text-gray-600">No stage counts.</div>
	{/if}

	<div class="mt-2">
		{#if needs === null}
			<div class="text-[11px] text-gray-600" title={needsReason ?? undefined}>
				Needs-you detection unavailable{needsReason ? ` (${needsReason})` : ''} — unknown, not "all clear".
			</div>
		{:else if needs.length === 0}
			<div class="text-[11px] text-gray-600">No strategy is waiting on you.</div>
		{:else}
			<div class="space-y-0.5">
				{#each needs.slice(0, 4) as entry}
					<div class="truncate text-[11px] text-amber-300" title={String(entry.top_blocker ?? '')}>
						● {entry.name ?? entry.strategy_id}: {entry.unblock_action ?? entry.status}
					</div>
				{/each}
			</div>
		{/if}
	</div>

	{#if events.length > 0}
		<div class="mt-2 space-y-0.5 border-t border-[#1a1a1a] pt-1.5">
			{#each events.slice(0, 3) as event}
				<div class="truncate text-[11px] text-gray-500" title={String(event.reason ?? '')}>
					{event.strategyId ?? event.strategy_id ?? '—'}: {event.fromState ?? event.from_state ?? '?'} →
					{event.toState ?? event.to_state ?? '?'}
				</div>
			{/each}
		</div>
	{/if}
</div>
