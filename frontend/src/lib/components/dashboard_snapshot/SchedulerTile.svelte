<script lang="ts">
	import { asList, asRecord, fmtAge } from './format';

	export let data: Record<string, unknown> | null;
	export let now: number;

	$: record = asRecord(data);
	$: jobs = asList(record.jobs);
	$: enabled = jobs.filter((job) => Boolean(job.enabled));
	$: overdueIds = Array.isArray(record.overdue_job_ids) ? record.overdue_job_ids : [];
	$: errorIds = Array.isArray(record.error_job_ids) ? record.error_job_ids : [];
	$: upcoming = enabled
		.filter((job) => typeof job.next_run_at === 'string')
		.sort((a, b) => String(a.next_run_at).localeCompare(String(b.next_run_at)))
		.slice(0, 3);
</script>

<div class="font-mono text-xs" data-testid="scheduler-tile">
	<div class="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1">
		<div class="text-gray-500">Jobs</div>
		<div class="text-gray-300">{enabled.length} enabled / {jobs.length}</div>

		<div class="text-gray-500">Overdue</div>
		<div class={overdueIds.length > 0 ? 'text-amber-400' : 'text-emerald-400'}>{overdueIds.length}</div>

		<div class="text-gray-500">Errored</div>
		<div class={errorIds.length > 0 ? 'text-red-400' : 'text-emerald-400'}>{errorIds.length}</div>
	</div>

	{#if upcoming.length > 0}
		<div class="mt-2 space-y-0.5">
			{#each upcoming as job}
				<div class="flex items-center justify-between text-[11px]">
					<span class="truncate text-gray-400" title={String(job.name ?? job.id ?? '')}>{job.name ?? job.id}</span>
					<span class="text-gray-500">{fmtAge(job.last_run_at, now)} ago</span>
				</div>
			{/each}
		</div>
	{/if}
</div>
