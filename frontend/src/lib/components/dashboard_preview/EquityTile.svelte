<script lang="ts">
	import { asRecord, fmtUsd } from './format';

	export let data: Record<string, unknown> | null;

	function pointValue(point: unknown): number | null {
		if (typeof point === 'number' && Number.isFinite(point)) return point;
		if (Array.isArray(point) && typeof point[1] === 'number') return point[1];
		if (point && typeof point === 'object') {
			const record = point as Record<string, unknown>;
			for (const key of ['equity', 'value', 'balance', 'y']) {
				if (typeof record[key] === 'number' && Number.isFinite(record[key] as number)) {
					return record[key] as number;
				}
			}
		}
		return null;
	}

	$: record = asRecord(data);
	$: values = (Array.isArray(record.curve) ? record.curve : [])
		.map(pointValue)
		.filter((value): value is number => value !== null);
	$: latest = values.length ? values[values.length - 1] : null;
	$: base = typeof record.base === 'number' ? record.base : values.length ? values[0] : null;
	$: delta = latest != null && base != null ? latest - base : null;
	$: min = values.length ? Math.min(...values) : 0;
	$: max = values.length ? Math.max(...values) : 1;
	$: span = max - min || 1;
	$: points = values
		.map((value, index) => {
			const x = values.length > 1 ? (index / (values.length - 1)) * 100 : 0;
			const y = 30 - ((value - min) / span) * 28 - 1;
			return `${x.toFixed(2)},${y.toFixed(2)}`;
		})
		.join(' ');
</script>

<div class="flex h-full flex-col font-mono text-xs" data-testid="equity-tile">
	<div class="flex items-baseline justify-between">
		<span class="text-gray-300">{fmtUsd(latest)}</span>
		{#if delta != null}
			<span class={delta >= 0 ? 'text-emerald-400' : 'text-red-400'}>
				{delta >= 0 ? '+' : ''}{fmtUsd(delta)}
			</span>
		{:else}
			<span class="text-gray-500">—</span>
		{/if}
	</div>
	{#if values.length > 1}
		<svg viewBox="0 0 100 30" preserveAspectRatio="none" class="mt-1 h-16 w-full flex-1">
			<polyline
				{points}
				fill="none"
				stroke={delta != null && delta < 0 ? '#f87171' : '#34d399'}
				stroke-width="0.8"
				vector-effect="non-scaling-stroke"
			/>
		</svg>
	{:else}
		<div class="mt-2 text-gray-600">No curve data.</div>
	{/if}
</div>
