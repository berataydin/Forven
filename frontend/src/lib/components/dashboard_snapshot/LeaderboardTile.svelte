<script lang="ts">
	import { asList, asRecord, fmtNum } from './format';

	export let data: Record<string, unknown> | null;

	$: record = asRecord(data);
	$: entries = asList(record.entries);

	function sharpe(entry: Record<string, unknown>): string {
		return fmtNum(entry.sharpe_ratio, 2);
	}
</script>

<div class="font-mono text-xs" data-testid="leaderboard-tile">
	{#if entries.length === 0}
		<div class="text-gray-600">No ranked strategies yet.</div>
	{:else}
		<div class="space-y-0.5">
			{#each entries.slice(0, 8) as entry, index}
				<div class="flex items-center justify-between gap-2 text-[11px]">
					<span class="flex min-w-0 items-center gap-2">
						<span class="w-4 shrink-0 text-right text-gray-600">{index + 1}</span>
						<span class="truncate text-gray-400" title={String(entry.display_name ?? entry.strategy_name ?? '')}>
							{entry.display_name ?? entry.strategy_name ?? entry.id}
						</span>
						<span class="shrink-0 text-gray-600">{entry.symbol ?? ''}</span>
					</span>
					<span class="shrink-0 text-gray-300">S {sharpe(entry)}</span>
				</div>
			{/each}
		</div>
	{/if}
</div>
