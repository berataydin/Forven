<script lang="ts">
	/**
	 * KPI strip on the snapshot's truth rules: fields the backend cannot
	 * measure arrive as null and render as "—" with the reason in a tooltip —
	 * never as 0 (the legacy overview hardcodes zeros for exactly these).
	 */
	import { asRecord, fmtNum } from './format';

	export let data: Record<string, unknown> | null;

	$: record = asRecord(data);
	$: kpis = asRecord(record.kpis);
	$: unknownFields = asRecord(record.unknown_fields);
	$: autopilot = asRecord(record.autopilot);

	function reason(field: string): string | undefined {
		const value = unknownFields[field];
		return typeof value === 'string' ? `unknown: ${value}` : undefined;
	}
</script>

<div class="flex flex-wrap items-center gap-x-6 gap-y-2 border border-[#222] bg-[#050505] px-3 py-2 font-mono text-xs" data-testid="kpis-strip">
	<div>
		<span class="text-gray-500">Tested</span>
		<span class="ml-1.5 text-gray-200">{fmtNum(kpis.total_tested)}</span>
	</div>
	<div>
		<span class="text-gray-500">Pipeline</span>
		<span class="ml-1.5 text-gray-200">{fmtNum(kpis.pipeline_count)}</span>
	</div>
	<div>
		<span class="text-gray-500">Best Sharpe</span>
		<span class="ml-1.5 text-gray-200">{fmtNum(kpis.best_sharpe, 2)}</span>
	</div>
	<div>
		<span class="text-gray-500">Scans</span>
		<span class="ml-1.5 text-gray-200">{fmtNum(kpis.active_scans)}</span>
	</div>
	<div title={reason('signals_today')}>
		<span class="text-gray-500">Signals today</span>
		<span class="ml-1.5 text-gray-200" data-testid="kpi-signals-today">{fmtNum(kpis.signals_today)}</span>
	</div>
	<div title={reason('data_coverage')}>
		<span class="text-gray-500">Coverage</span>
		<span class="ml-1.5 text-gray-200" data-testid="kpi-data-coverage">{fmtNum(kpis.data_coverage)}</span>
	</div>
	<div class="ml-auto flex items-center gap-1.5" title={typeof autopilot.disabled_reason === 'string' ? autopilot.disabled_reason : undefined}>
		<span
			class={`h-1.5 w-1.5 rounded-full ${autopilot.running ? 'bg-emerald-400' : Object.keys(autopilot).length ? 'bg-red-500' : 'bg-[#333]'}`}
		></span>
		<span class="text-[10px] uppercase tracking-wider text-gray-500">
			Autopilot {Object.keys(autopilot).length === 0 ? '—' : autopilot.running ? 'running' : 'stopped'}
		</span>
	</div>
</div>
