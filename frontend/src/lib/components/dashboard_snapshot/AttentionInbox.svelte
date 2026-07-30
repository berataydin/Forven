<script lang="ts">
	/**
	 * "Needs attention now" — derived state from the snapshot, severity-ordered
	 * server-side. No ack/dismiss by design (locked decision 4): items resolve
	 * when their source condition clears. Every critical item carries an action.
	 */
	import type { InboxItem } from '$lib/api/snapshot';
	import { fmtAge } from './format';

	export let items: InboxItem[] = [];
	export let now: number;

	const SEVERITY_STYLES: Record<string, string> = {
		critical: 'border-red-800 bg-red-500/10 text-red-300',
		warning: 'border-amber-800 bg-amber-500/5 text-amber-200',
		info: 'border-[#333] bg-[#0a0a0a] text-gray-400',
	};
	const CHIP_STYLES: Record<string, string> = {
		critical: 'bg-red-500/20 text-red-400 border-red-800',
		warning: 'bg-amber-500/10 text-amber-400 border-amber-800',
		info: 'bg-[#111] text-gray-500 border-[#333]',
	};
</script>

<div data-testid="attention-inbox">
	{#if items.length === 0}
		<div
			class="flex items-center gap-2 border border-emerald-900/60 bg-emerald-500/5 px-3 py-2 text-xs text-emerald-400"
			data-testid="attention-inbox-empty"
		>
			<span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span>
			Nothing needs you right now.
		</div>
	{:else}
		<div class="space-y-1">
			{#each items as item (item.id)}
				<div
					class={`flex items-center justify-between gap-3 border px-3 py-1.5 ${SEVERITY_STYLES[item.severity] ?? SEVERITY_STYLES.info}`}
					data-testid="attention-item"
					data-item-id={item.id}
				>
					<div class="flex min-w-0 items-center gap-2.5">
						<span
							class={`shrink-0 border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider ${CHIP_STYLES[item.severity] ?? CHIP_STYLES.info}`}
							>{item.severity}</span
						>
						<span class="truncate text-xs" title={item.message}>{item.message}</span>
						<span class="shrink-0 text-[10px] text-[#555]" title="first observed"
							>for {fmtAge(item.first_observed_at, now)}</span
						>
					</div>
					{#if item.action_href && item.action_label}
						<a
							href={item.action_href}
							class="shrink-0 border border-[#444] px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-gray-300 hover:border-white hover:text-white"
							data-testid="attention-item-action">{item.action_label}</a
						>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>
