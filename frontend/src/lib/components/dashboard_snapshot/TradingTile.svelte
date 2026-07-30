<script lang="ts">
	import { asRecord, fmtPct, fmtUsd } from './format';

	export let data: Record<string, unknown> | null;

	$: record = asRecord(data);
	$: risk = asRecord(record.risk);
	$: dailyRisk = asRecord(record.daily_risk);
	$: account = asRecord(record.account);
	$: startEquity = typeof dailyRisk.start_equity === 'number' ? dailyRisk.start_equity : null;
	$: currentEquity = typeof dailyRisk.current_equity === 'number' ? dailyRisk.current_equity : null;
	$: dayPnl = startEquity != null && currentEquity != null ? currentEquity - startEquity : null;
	$: dayPnlPct = dayPnl != null && startEquity ? (dayPnl / startEquity) * 100 : null;
	$: halted = Boolean(risk.kill_switch_active) || Boolean(risk.daily_loss_halt);
	$: drawdown = typeof risk.drawdown_pct === 'number' ? risk.drawdown_pct * 100 : null;
	$: mode = typeof record.execution_mode === 'string' ? record.execution_mode : null;
</script>

<div class="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1 font-mono text-xs" data-testid="trading-tile">
	<div class="text-gray-500">Mode</div>
	<div class="uppercase {mode === 'live' ? 'text-red-400' : 'text-gray-300'}">
		{mode ?? '—'}{record.simulation_active ? ' · sim' : ''}
	</div>

	<div class="text-gray-500">Halts</div>
	<div class={halted ? 'font-bold text-red-400' : 'text-emerald-400'} data-testid="trading-halts">
		{#if risk.kill_switch_active}KILL SWITCH{/if}
		{#if risk.kill_switch_active && risk.daily_loss_halt}&nbsp;+&nbsp;{/if}
		{#if risk.daily_loss_halt}DAILY LOSS HALT{/if}
		{#if !halted}none{/if}
	</div>

	<div class="text-gray-500">Account value</div>
	<div class="text-gray-300">{fmtUsd(account.accountValue)}</div>

	<div class="text-gray-500">Equity (day)</div>
	<div class="text-gray-300">
		{fmtUsd(currentEquity)}
		{#if dayPnl != null}
			<span class={dayPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>
				({dayPnl >= 0 ? '+' : ''}{fmtUsd(dayPnl)}{dayPnlPct != null ? ` / ${dayPnlPct >= 0 ? '+' : ''}${fmtPct(dayPnlPct)}` : ''})
			</span>
		{/if}
	</div>

	<div class="text-gray-500">Drawdown</div>
	<div class={drawdown != null && drawdown >= 8 ? 'text-amber-400' : 'text-gray-300'}>{fmtPct(drawdown)}</div>
</div>
