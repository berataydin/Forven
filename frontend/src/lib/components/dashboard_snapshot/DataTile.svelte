<script lang="ts">
	import { asRecord, fmtAge, fmtNum } from './format';

	export let data: Record<string, unknown> | null;
	export let now: number;

	$: record = asRecord(data);
	$: quality = typeof record.quality_avg_score === 'number' ? record.quality_avg_score : null;
	$: orphans = typeof record.orphan_count === 'number' ? record.orphan_count : null;
</script>

<div class="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1 font-mono text-xs" data-testid="data-tile">
	<div class="text-gray-500">Datasets</div>
	<div class="text-gray-300">{fmtNum(record.dataset_count)}</div>

	<div class="text-gray-500">Last ingestion</div>
	<div class="text-gray-300">{fmtAge(record.last_ingestion_at, now)} ago</div>

	<div class="text-gray-500">Quality avg</div>
	<div
		class={quality == null
			? 'text-gray-500'
			: quality >= 90
				? 'text-emerald-400'
				: quality >= 70
					? 'text-amber-400'
					: 'text-red-400'}
	>
		{quality == null ? '—' : Math.round(quality)}
	</div>

	<div class="text-gray-500">Orphans</div>
	<div class={orphans != null && orphans > 0 ? 'text-amber-400' : orphans == null ? 'text-gray-500' : 'text-gray-300'}>
		{fmtNum(orphans)}
	</div>
</div>
