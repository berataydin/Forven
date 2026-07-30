<script lang="ts">
	import { asList, asRecord, fmtNum, fmtPct, fmtUsd } from './format';

	export let data: Record<string, unknown> | null;

	$: record = asRecord(data);
	$: totals = asRecord(record.totals);
	$: sessions = asList(record.sessions);
	$: pnl = typeof totals.realized_pnl_usd === 'number' ? totals.realized_pnl_usd : null;
</script>

<div class="font-mono text-xs" data-testid="paper-tile">
	<div class="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1">
		<div class="text-gray-500">Sessions</div>
		<div class="text-gray-300">
			{fmtNum(totals.session_count)}
			<span class="text-gray-500">({fmtNum(totals.open_count)} open)</span>
		</div>

		<div class="text-gray-500">Realized PnL</div>
		<div class={pnl == null ? 'text-gray-500' : pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>
			{fmtUsd(pnl)}
		</div>

		<div class="text-gray-500">Win rate</div>
		<div class="text-gray-300">{fmtPct(totals.win_rate_pct)}</div>
	</div>

	{#if sessions.length > 0}
		<div class="mt-2 space-y-0.5">
			{#each sessions.slice(0, 4) as session}
				<div class="flex items-center justify-between gap-2 text-[11px]">
					<span class="truncate text-gray-400" title={String(session.strategy_name ?? session.strategy_id ?? '')}>
						{session.strategy_name ?? session.strategy_id ?? '—'}
						<span class="text-gray-600">{session.symbol ?? ''}</span>
					</span>
					<span
						class={typeof session.realized_pnl_usd === 'number'
							? (session.realized_pnl_usd as number) >= 0
								? 'text-emerald-400'
								: 'text-red-400'
							: 'text-gray-500'}>{fmtUsd(session.realized_pnl_usd)}</span
					>
				</div>
			{/each}
		</div>
	{/if}
</div>
