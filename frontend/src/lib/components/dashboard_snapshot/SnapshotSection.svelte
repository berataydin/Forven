<script lang="ts">
	/**
	 * Truthful-state wrapper — the convention that kills the fail-open display
	 * class (unknown must never look like safe/off):
	 * - fresh: subtle age stamp only
	 * - stale: amber STALE chip + age
	 * - error: red FETCH FAILED chip; last-good data stays visible with its age
	 * - unavailable: explicit UNAVAILABLE chip + error code; body shows "no data"
	 * - client offline (browser cannot reach the backend): red OFFLINE chip on
	 *   every section, over whatever last-good data the store retained
	 */
	import type { SnapshotSectionPayload } from '$lib/api/snapshot';
	import { fmtAge } from './format';

	export let title: string;
	export let section: SnapshotSectionPayload | undefined = undefined;
	export let now: number;
	export let clientOffline = false;
	export let href: string | null = null;
	export let hrefLabel = 'open →';
	export let testid: string;

	$: status = section?.status ?? 'unavailable';
	$: hasData = section?.data != null;
	$: ageText = fmtAge(section?.as_of ?? null, now);
</script>

<div class="flex h-full flex-col border border-[#222] bg-[#050505] p-2.5" data-testid={testid}>
	<div class="mb-1.5 flex items-center justify-between gap-2">
		<h2 class="text-[10px] font-semibold uppercase tracking-wider text-gray-500">{title}</h2>
		<div class="flex items-center gap-2">
			{#if clientOffline}
				<span
					class="border border-red-800 bg-red-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-red-400"
					data-testid="{testid}-chip-offline">OFFLINE · last {ageText}</span
				>
			{:else if status === 'stale'}
				<span
					class="border border-amber-800 bg-amber-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-amber-400"
					data-testid="{testid}-chip-stale">STALE · {ageText}</span
				>
			{:else if status === 'error'}
				<span
					class="border border-red-800 bg-red-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-red-400"
					title={section?.error_code ?? undefined}
					data-testid="{testid}-chip-error">FETCH FAILED · showing {ageText} old</span
				>
			{:else if status === 'unavailable'}
				<span
					class="border border-[#333] bg-[#111] px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-gray-500"
					title={section?.error_code ?? undefined}
					data-testid="{testid}-chip-unavailable">UNAVAILABLE{section?.error_code ? ` · ${section.error_code}` : ''}</span
				>
			{:else}
				<span class="text-[9px] uppercase tracking-wider text-[#444]" data-testid="{testid}-chip-fresh"
					>as of {ageText} ago</span
				>
			{/if}
			{#if href}
				<a {href} class="text-[10px] uppercase text-[#555] hover:text-white">{hrefLabel}</a>
			{/if}
		</div>
	</div>

	{#if hasData}
		<slot data={section?.data ?? null} />
	{:else}
		<div class="text-xs text-gray-600" data-testid="{testid}-nodata">
			No data yet{section?.error_code ? ` (${section.error_code})` : ''} — unknown, not zero.
		</div>
	{/if}
</div>
