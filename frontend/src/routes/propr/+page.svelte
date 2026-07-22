<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		cancelProprOrder,
		clearProprApiKey,
		closeProprPosition,
		getProprMirror,
		getProprOverview,
		getProprStatus,
		runProprConnectionTest,
		setProprApiKey,
		tickProprMirror,
		updateProprMirror,
		type ProprConnectionCheck,
		type ProprMirror,
		type ProprOverview,
		type ProprStatus,
	} from '$lib/api/propr';
	import ErrorBanner from '$lib/components/ErrorBanner.svelte';
	import LoadingState from '$lib/components/LoadingState.svelte';

	let overview: ProprOverview | null = null;
	let status: ProprStatus | null = null;
	let loading = true;
	let error = '';
	let actionMessage = '';
	let refreshTimer: ReturnType<typeof setInterval> | null = null;

	// PROPR-1: while the hidden backend flag is off, every route except status
	// 404s and status reports { enabled: false } — render the neutral off state
	// (deliberately without naming the flag; this page must not document its
	// own activation).
	let enabled: boolean | null = null;

	let apiKeyInput = '';
	let keyBusy = false;
	let testBusy = false;
	let testChecks: ProprConnectionCheck[] | null = null;
	let closingPosition: string | null = null;
	let confirmingClose: string | null = null;
	let cancellingOrder: string | null = null;

	// --- strategy mirror ----------------------------------------------------
	let mirror: ProprMirror | null = null;
	let rosterDraft: Set<string> = new Set();
	let rosterDirty = false;
	let mirrorBusy = false;
	let mirrorTickBusy = false;
	let strategyFilter = '';

	type Row = Record<string, unknown>;
	// Propr response field names are matched permissively — the docs don't pin
	// every key, so each cell reads the first present spelling.
	const pick = (row: Row, ...keys: string[]): unknown => {
		for (const key of keys) {
			const value = row?.[key];
			if (value !== undefined && value !== null && value !== '') return value;
		}
		return null;
	};
	const pickStr = (row: Row, ...keys: string[]): string => {
		const value = pick(row, ...keys);
		return value === null ? '—' : String(value);
	};
	const pickNum = (row: Row, ...keys: string[]): number | null => {
		const value = pick(row, ...keys);
		if (value === null) return null;
		const num = Number(value);
		return Number.isNaN(num) ? null : num;
	};
	const fmtNum = (v: number | null | undefined, digits = 2) =>
		v === null || v === undefined || Number.isNaN(v) ? '—' : v.toFixed(digits);
	const fmtUsd = (v: number | null | undefined) => {
		if (v === null || v === undefined || Number.isNaN(v)) return '—';
		return `${v < 0 ? '−' : ''}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
	};
	const fmtWhen = (value: unknown) => {
		if (!value) return '—';
		try {
			return new Date(String(value)).toLocaleString();
		} catch {
			return String(value);
		}
	};

	async function load() {
		try {
			status = await getProprStatus(false);
			enabled = Boolean(status?.enabled);
			if (!enabled) {
				loading = false;
				return;
			}
			const [ov, mi] = await Promise.all([
				getProprOverview(),
				getProprMirror().catch(() => null),
			]);
			overview = ov;
			status = ov.status;
			mirror = mi;
			if (mi && !rosterDirty) rosterDraft = new Set(Object.keys(mi.strategies));
			error = '';
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		load();
		refreshTimer = setInterval(load, 30_000);
	});
	onDestroy(() => {
		if (refreshTimer) clearInterval(refreshTimer);
	});

	async function saveKey() {
		keyBusy = true;
		actionMessage = '';
		try {
			await setProprApiKey(apiKeyInput.trim());
			apiKeyInput = '';
			actionMessage = 'API key stored (encrypted). Run the connection test to verify it.';
			await load();
		} catch (e) {
			actionMessage = `Could not store the key: ${e instanceof Error ? e.message : e}`;
		} finally {
			keyBusy = false;
		}
	}

	async function clearKey() {
		keyBusy = true;
		actionMessage = '';
		try {
			await clearProprApiKey();
			actionMessage = 'API key removed.';
			testChecks = null;
			await load();
		} catch (e) {
			actionMessage = `Could not remove the key: ${e instanceof Error ? e.message : e}`;
		} finally {
			keyBusy = false;
		}
	}

	async function runTest() {
		testBusy = true;
		actionMessage = '';
		testChecks = null;
		try {
			const res = await runProprConnectionTest();
			testChecks = res.checks;
			actionMessage = res.ok
				? 'Connection test passed — key, account and positions all read cleanly.'
				: 'Connection test finished with failures — see the checks below.';
		} catch (e) {
			actionMessage = `Connection test failed: ${e instanceof Error ? e.message : e}`;
		} finally {
			testBusy = false;
		}
	}

	function toggleRosterStrategy(id: string) {
		if (rosterDraft.has(id)) rosterDraft.delete(id);
		else rosterDraft.add(id);
		rosterDraft = new Set(rosterDraft);
		rosterDirty = true;
	}

	async function saveRoster() {
		mirrorBusy = true;
		actionMessage = '';
		try {
			await updateProprMirror({ strategies: [...rosterDraft] });
			rosterDirty = false;
			actionMessage = `Mirror roster saved (${rosterDraft.size} strateg${rosterDraft.size === 1 ? 'y' : 'ies'}). Only trades opened from now on are mirrored.`;
			await load();
		} catch (e) {
			actionMessage = `Roster save failed: ${e instanceof Error ? e.message : e}`;
		} finally {
			mirrorBusy = false;
		}
	}

	async function toggleMirror() {
		mirrorBusy = true;
		actionMessage = '';
		try {
			const next = !mirror?.enabled;
			await updateProprMirror({ enabled: next });
			actionMessage = next
				? 'Mirror enabled — the observer copies new roster trades to Propr every 60s.'
				: 'Mirror disabled — no new Propr orders; existing mirrored positions keep their brackets.';
			await load();
		} catch (e) {
			actionMessage = `Mirror toggle failed: ${e instanceof Error ? e.message : e}`;
		} finally {
			mirrorBusy = false;
		}
	}

	async function forceMirrorTick() {
		mirrorTickBusy = true;
		actionMessage = '';
		try {
			const res = await tickProprMirror();
			actionMessage = `Mirror tick: ${JSON.stringify(res.result)}`;
			await load();
		} catch (e) {
			actionMessage = `Mirror tick failed: ${e instanceof Error ? e.message : e}`;
		} finally {
			mirrorTickBusy = false;
		}
	}

	async function closePos(row: Row) {
		const asset = pickStr(row, 'asset', 'coin');
		const side = pickStr(row, 'positionSide', 'side').toLowerCase() === 'short' ? 'short' : 'long';
		const qty = Math.abs(pickNum(row, 'quantity', 'size', 'szi') ?? 0);
		const key = `${asset}:${side}`;
		if (confirmingClose !== key) {
			confirmingClose = key;
			return;
		}
		confirmingClose = null;
		closingPosition = key;
		actionMessage = '';
		try {
			await closeProprPosition(asset, side, qty);
			actionMessage = `Close sent for ${asset} ${side} (${qty}).`;
			await load();
		} catch (e) {
			actionMessage = `Close refused: ${e instanceof Error ? e.message : e}`;
		} finally {
			closingPosition = null;
		}
	}

	async function cancelOrder(row: Row) {
		const orderId = pickStr(row, 'orderId', 'order_id', 'id');
		cancellingOrder = orderId;
		actionMessage = '';
		try {
			await cancelProprOrder(orderId);
			actionMessage = `Cancel sent for order ${orderId}.`;
			await load();
		} catch (e) {
			actionMessage = `Cancel refused: ${e instanceof Error ? e.message : e}`;
		} finally {
			cancellingOrder = null;
		}
	}

	$: candidates = (mirror?.candidates ?? []).filter(
		(c) =>
			!strategyFilter.trim() ||
			`${c.id} ${c.name}`.toLowerCase().includes(strategyFilter.trim().toLowerCase())
	);
	$: mirrorRows = Object.entries(mirror?.state ?? {}).sort(
		(a, b) => (b[1].opened_at ?? '').localeCompare(a[1].opened_at ?? '')
	);
	$: accountIsPaper = status?.account_type === 'paper';
	$: positions = (overview?.positions ?? []) as Row[];
	$: openOrders = ((overview?.orders ?? []) as Row[]).filter((o) =>
		['pending', 'open', 'partially_filled'].includes(pickStr(o, 'status').toLowerCase())
	);
	$: trades = ((overview?.trades ?? []) as Row[]).slice(0, 25);
	$: attempts = (overview?.attempts ?? []) as Row[];
	$: sectionErrors = overview?.errors ?? {};
</script>

<svelte:head>
	<title>Propr — Forven</title>
</svelte:head>

<div class="space-y-4 p-4">
	<div class="flex items-center justify-between">
		<div>
			<h1 class="text-lg font-bold uppercase tracking-wider text-white">Propr</h1>
			<p class="text-[11px] text-[#666]">
				On-chain prop firm on Hyperliquid: a purchased challenge account trades real HL markets
				through Propr's API. This page manages the connection, the challenge account, and manual
				position control. Live dispatch only reaches Propr when explicitly armed on the backend.
			</p>
		</div>
		{#if status?.enabled}
			<div class="flex items-center gap-2 shrink-0">
				<span
					class={`text-xs px-2 py-1 border ${status.connected ? 'text-emerald-400 border-emerald-800' : 'text-yellow-400 border-yellow-800'}`}
				>
					{status.connected ? 'Connected' : status.api_key_configured ? 'Not connected' : 'No API key'}
				</span>
				{#if status.account_type}
					<span
						class={`text-xs px-2 py-1 border ${accountIsPaper ? 'text-[#c9a227] border-[#8a6d1a]' : 'text-red-400 border-red-800'}`}
						title={accountIsPaper
							? 'Propr reports this account as a paper/trial account — mirrored orders spend no real money.'
							: 'Propr reports this account as REAL — orders require the backend live opt-in.'}
					>
						{accountIsPaper ? 'PAPER ACCOUNT' : `REAL (${status.account_type})`}
					</span>
				{/if}
				<span
					class={`text-xs px-2 py-1 border ${mirror?.enabled ? 'text-emerald-400 border-emerald-800' : 'text-[#666] border-[#333]'}`}
				>
					{mirror?.enabled ? 'Mirror ON' : 'Mirror off'}
				</span>
			</div>
		{/if}
	</div>

	{#if error}
		<ErrorBanner message={error} />
	{/if}
	{#if actionMessage}
		<div class="border border-[#333] bg-[#0a0a0a] px-3 py-2 text-xs text-[#aaa]">{actionMessage}</div>
	{/if}

	{#if loading}
		<LoadingState message="Loading Propr…" />
	{:else if enabled === false}
		<div class="border border-[#222] bg-[#050505] p-6 text-center space-y-2">
			<div class="text-sm font-bold uppercase tracking-wider text-[#888]">
				Propr integration is not enabled
			</div>
			<p class="text-xs text-[#666]">
				This integration is switched off on the backend and has no Settings-page control.
			</p>
		</div>
	{:else}
		<!-- ─────────────────────── connection ─────────────────────── -->
		<div class="border border-[#222] bg-[#050505] p-4 space-y-3">
			<div class="flex items-center justify-between">
				<h2 class="text-sm font-bold uppercase tracking-wider text-white">Connection</h2>
				<span class="text-[10px] text-[#666]">{status?.base_url}</span>
			</div>
			{#if status?.connected && !status?.orders_allowed}
				<div class="border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-[11px] text-yellow-500">
					Order placement is DISARMED: the account is not verifiably a paper/trial account
					(type: {status?.account_type ?? 'unknown'}) and the backend live opt-in is not set.
					Reads work; every open/close is refused. This is the designed behavior for when the
					evaluation ends and the account becomes real.
				</div>
			{:else if accountIsPaper && !status?.allow_live}
				<div class="border border-[#1a2438] bg-[#050a12] px-3 py-2 text-[11px] text-[#9ab]">
					Orders are allowed WITHOUT the backend live opt-in because Propr reports this account
					as paper — verified against the exchange every few minutes. The moment the evaluation
					ends and the account type changes, order placement fails closed automatically.
				</div>
			{/if}
			<div class="grid grid-cols-1 md:grid-cols-3 gap-2 items-end text-[11px]">
				<label class="space-y-1 md:col-span-2">
					<span class="text-[#666] uppercase text-[10px] tracking-wider">
						API key {status?.api_key_configured ? '(configured — paste to replace)' : '(from app.propr.xyz → Settings)'}
					</span>
					<input
						type="password"
						bind:value={apiKeyInput}
						placeholder="pk_live_…"
						autocomplete="off"
						class="w-full border border-[#333] bg-[#0a0a0a] px-2 py-1.5 text-white outline-none"
					/>
				</label>
				<div class="flex items-center gap-2">
					<button
						class="border border-[#333] bg-[#111] px-3 py-1.5 text-xs text-[#aaa] hover:bg-[#1a1a1a] disabled:opacity-50"
						on:click={saveKey}
						disabled={keyBusy || !apiKeyInput.trim()}
					>
						{keyBusy ? 'Saving…' : 'Save key'}
					</button>
					{#if status?.api_key_configured}
						<button
							class="border border-[#333] bg-[#111] px-3 py-1.5 text-xs text-[#888] hover:bg-[#1a1a1a] disabled:opacity-50"
							on:click={clearKey}
							disabled={keyBusy}
						>
							Remove
						</button>
					{/if}
					<button
						class="border border-[#333] bg-[#111] px-3 py-1.5 text-xs text-[#aaa] hover:bg-[#1a1a1a] disabled:opacity-50"
						on:click={runTest}
						disabled={testBusy || !status?.api_key_configured}
					>
						{testBusy ? 'Testing…' : 'Connection test'}
					</button>
				</div>
			</div>
			{#if testChecks}
				<div class="space-y-1">
					{#each testChecks as check (check.name)}
						<div class="flex items-center gap-2 text-[11px]">
							<span class={check.ok ? 'text-emerald-400' : 'text-red-400'}>{check.ok ? '✓' : '✗'}</span>
							<span class="uppercase text-[10px] tracking-wider text-[#888] w-20">{check.name}</span>
							<span class="text-[#666] truncate font-mono text-[10px]">
								{check.ok ? 'ok' : String(check.detail)}
							</span>
						</div>
					{/each}
				</div>
			{/if}
		</div>

		<!-- ─────────────────────── challenge account ─────────────────────── -->
		<div class="border border-[#222] bg-[#050505] p-4 space-y-3">
			<h2 class="text-sm font-bold uppercase tracking-wider text-white">Challenge account</h2>
			{#if sectionErrors.attempts}
				<div class="text-[11px] text-yellow-500">{sectionErrors.attempts}</div>
			{/if}
			<div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-center">
				<div class="border border-[#222] bg-[#0a0a0a] p-2">
					<div class="text-[10px] uppercase tracking-wider text-[#666]">Account value</div>
					<div class="text-base font-bold text-white">{fmtUsd(status?.account_value)}</div>
				</div>
				<div class="border border-[#222] bg-[#0a0a0a] p-2">
					<div class="text-[10px] uppercase tracking-wider text-[#666]">Attempt status</div>
					<div class="text-base font-bold text-[#aaa]">{status?.attempt_status ?? '—'}</div>
				</div>
				<div class="border border-[#222] bg-[#0a0a0a] p-2">
					<div class="text-[10px] uppercase tracking-wider text-[#666]">Account</div>
					<div class="text-[10px] font-mono text-[#888] pt-1.5 truncate" title={status?.account_id}>
						{status?.account_id ?? '—'}
					</div>
				</div>
				<div class="border border-[#222] bg-[#0a0a0a] p-2">
					<div class="text-[10px] uppercase tracking-wider text-[#666]">Open positions</div>
					<div class="text-base font-bold text-white">{positions.length}</div>
				</div>
			</div>
			{#if attempts.length > 0}
				<details class="text-[11px]">
					<summary class="cursor-pointer text-[#666] hover:text-[#888] uppercase text-[10px] tracking-wider">
						Challenge attempts ({attempts.length})
					</summary>
					<div class="mt-1 space-y-0.5 max-h-40 overflow-y-auto">
						{#each attempts as attempt}
							<div class="flex items-center gap-3 border-b border-[#141414] py-1 text-[#888]">
								<span class="font-mono text-[10px] truncate">{pickStr(attempt, 'id', 'attemptId')}</span>
								<span class="uppercase text-[10px]">{pickStr(attempt, 'status')}</span>
								<span class="text-[#555] text-[10px]">{fmtWhen(pick(attempt, 'createdAt', 'created_at'))}</span>
							</div>
						{/each}
					</div>
				</details>
			{/if}
		</div>

		<!-- ─────────────────────── strategy mirror ─────────────────────── -->
		<div
			class={`border p-4 space-y-3 ${mirror?.enabled ? 'border-emerald-900 bg-[#050805]' : 'border-[#222] bg-[#050505]'}`}
		>
			<div class="flex items-center justify-between">
				<div>
					<h2 class="text-sm font-bold uppercase tracking-wider text-white">Strategy mirror</h2>
					<p class="text-[11px] text-[#666]">
						Pick the strategies whose trades get copied onto the Propr account. Live and paper
						trading are completely untouched — the mirror only observes their trades and places
						its own independently-sized orders here (risk % of the challenge equity at each
						trade's stop, entry + stop + take-profit as one bracket, reduce-only closes). Only
						trades opened AFTER a strategy joins the roster are mirrored.
					</p>
				</div>
				<div class="flex items-center gap-2 shrink-0">
					<button
						class={`border px-3 py-1.5 text-xs disabled:opacity-50 ${
							mirror?.enabled
								? 'border-emerald-800 text-emerald-400 bg-[#081008] hover:bg-[#0a140a]'
								: 'border-[#333] bg-[#111] text-[#aaa] hover:bg-[#1a1a1a]'
						}`}
						on:click={toggleMirror}
						disabled={mirrorBusy || !status?.api_key_configured}
					>
						{mirror?.enabled ? 'Mirror is ON — click to disable' : 'Enable mirror'}
					</button>
					<button
						class="border border-[#333] bg-[#111] px-3 py-1.5 text-xs text-[#aaa] hover:bg-[#1a1a1a] disabled:opacity-50"
						on:click={forceMirrorTick}
						disabled={mirrorTickBusy || !mirror?.enabled}
					>
						{mirrorTickBusy ? 'Ticking…' : 'Tick now'}
					</button>
				</div>
			</div>

			<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
				<div class="space-y-2">
					<div class="flex items-center justify-between">
						<span class="text-[10px] uppercase tracking-wider text-[#666]">
							Strategies ({rosterDraft.size} selected)
						</span>
						<input
							type="text"
							bind:value={strategyFilter}
							placeholder="filter…"
							class="border border-[#333] bg-[#0a0a0a] px-2 py-1 text-[11px] text-white outline-none w-32"
						/>
					</div>
					<div class="max-h-64 overflow-y-auto border border-[#1a1a1a] divide-y divide-[#141414]">
						{#each candidates as c (c.id)}
							<label class="flex items-center gap-2 px-2 py-1.5 text-[11px] cursor-pointer hover:bg-[#0d0d0d]">
								<input
									type="checkbox"
									checked={rosterDraft.has(c.id)}
									on:change={() => toggleRosterStrategy(c.id)}
								/>
								<span class="font-bold text-[#bbb]">{c.id}</span>
								<span class="text-[#777] truncate flex-1">{c.name}</span>
								<span
									class={`text-[9px] uppercase tracking-wider px-1 py-0.5 border ${
										c.stage === 'live_graduated'
											? 'border-red-900 text-red-400'
											: c.stage === 'paper'
												? 'border-[#1c2b1c] text-emerald-500'
												: 'border-[#333] text-[#777]'
									}`}
								>
									{c.stage}
								</span>
								{#if c.timeframe}<span class="text-[#555] text-[10px]">{c.timeframe}</span>{/if}
							</label>
						{:else}
							<div class="px-2 py-3 text-[11px] text-[#555]">No strategies match the filter.</div>
						{/each}
					</div>
					{#if rosterDirty}
						<button
							class="border border-emerald-900 bg-[#081008] px-3 py-1.5 text-xs text-emerald-400 hover:bg-[#0a140a] disabled:opacity-50"
							on:click={saveRoster}
							disabled={mirrorBusy}
						>
							{mirrorBusy ? 'Saving…' : `Save roster (${rosterDraft.size})`}
						</button>
					{/if}
				</div>

				<div class="space-y-1">
					<span class="text-[10px] uppercase tracking-wider text-[#666]">Mirrored trades</span>
					{#if mirrorRows.length === 0}
						<div class="text-[11px] text-[#555]">
							Nothing mirrored yet — trades appear here within a minute of a roster strategy
							opening one.
						</div>
					{:else}
						<div class="max-h-64 overflow-y-auto space-y-0.5">
							{#each mirrorRows as [tradeId, st] (tradeId)}
								<div class="border border-[#151515] bg-[#0a0a0a] px-2 py-1 text-[11px] flex items-center gap-2">
									<span
										class={`text-[9px] uppercase tracking-wider px-1 py-0.5 border shrink-0 ${
											st.status === 'open'
												? 'border-emerald-800 text-emerald-400'
												: st.status === 'closed'
													? 'border-[#333] text-[#888]'
													: st.status === 'skipped'
														? 'border-[#333] text-[#666]'
														: 'border-red-900 text-red-400'
										}`}
									>
										{st.status}
									</span>
									<span class="font-bold text-[#bbb] shrink-0">{st.asset}</span>
									<span class={st.direction === 'short' ? 'text-red-400' : 'text-emerald-400'}>
										{st.direction}
									</span>
									<span class="text-[#777] truncate flex-1" title={st.reason ?? ''}>
										{st.strategy}{st.reason ? ` — ${st.reason}` : ''}
									</span>
									{#if st.quantity}<span class="text-[#666]">{fmtNum(st.quantity, 5)}</span>{/if}
								</div>
							{/each}
						</div>
					{/if}
				</div>
			</div>
		</div>

		<!-- ─────────────────────── positions ─────────────────────── -->
		<div class="border border-[#222] bg-[#050505] p-4 space-y-2">
			<h2 class="text-sm font-bold uppercase tracking-wider text-white">Positions</h2>
			{#if sectionErrors.positions}
				<div class="text-[11px] text-yellow-500">{sectionErrors.positions}</div>
			{:else if positions.length === 0}
				<div class="text-[11px] text-[#555]">No open positions.</div>
			{:else}
				<div class="overflow-x-auto">
					<table class="w-full text-[11px]">
						<thead>
							<tr class="text-left text-[10px] uppercase tracking-wider text-[#666] border-b border-[#222]">
								<th class="py-1 pr-2">Asset</th>
								<th class="py-1 pr-2">Side</th>
								<th class="py-1 pr-2 text-right">Size</th>
								<th class="py-1 pr-2 text-right">Entry</th>
								<th class="py-1 pr-2 text-right">Mark</th>
								<th class="py-1 pr-2 text-right">Liq.</th>
								<th class="py-1 pr-2 text-right">uPnL</th>
								<th class="py-1 text-right">Actions</th>
							</tr>
						</thead>
						<tbody>
							{#each positions as row}
								{@const key = `${pickStr(row, 'asset', 'coin')}:${pickStr(row, 'positionSide', 'side').toLowerCase()}`}
								{@const upnl = pickNum(row, 'unrealizedPnl', 'unrealized_pnl')}
								<tr class="border-b border-[#151515] text-[#999]">
									<td class="py-1 pr-2 font-bold text-[#bbb]">{pickStr(row, 'asset', 'coin')}</td>
									<td class="py-1 pr-2">
										<span class={pickStr(row, 'positionSide', 'side').toLowerCase() === 'short' ? 'text-red-400' : 'text-emerald-400'}>
											{pickStr(row, 'positionSide', 'side')}
										</span>
									</td>
									<td class="py-1 pr-2 text-right">{fmtNum(pickNum(row, 'quantity', 'size', 'szi'), 5)}</td>
									<td class="py-1 pr-2 text-right">{fmtNum(pickNum(row, 'entryPrice', 'entry_price'))}</td>
									<td class="py-1 pr-2 text-right">{fmtNum(pickNum(row, 'markPrice', 'mark_price'))}</td>
									<td class="py-1 pr-2 text-right text-[#777]">{fmtNum(pickNum(row, 'liquidationPrice', 'liquidation_price'))}</td>
									<td class={`py-1 pr-2 text-right ${(upnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
										{fmtUsd(upnl)}
									</td>
									<td class="py-1 text-right">
										<button
											class={`border px-2 py-0.5 text-[10px] uppercase tracking-wider disabled:opacity-50 ${
												confirmingClose === key
													? 'border-red-700 bg-red-950 text-red-300'
													: 'border-[#333] text-[#888] hover:text-white'
											}`}
											on:click={() => closePos(row)}
											disabled={closingPosition === key}
										>
											{closingPosition === key ? 'Closing…' : confirmingClose === key ? 'Confirm close' : 'Close'}
										</button>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>

		<!-- ─────────────────────── open orders ─────────────────────── -->
		<div class="border border-[#222] bg-[#050505] p-4 space-y-2">
			<h2 class="text-sm font-bold uppercase tracking-wider text-white">Open orders</h2>
			{#if sectionErrors.orders}
				<div class="text-[11px] text-yellow-500">{sectionErrors.orders}</div>
			{:else if openOrders.length === 0}
				<div class="text-[11px] text-[#555]">No open orders.</div>
			{:else}
				<div class="overflow-x-auto">
					<table class="w-full text-[11px]">
						<thead>
							<tr class="text-left text-[10px] uppercase tracking-wider text-[#666] border-b border-[#222]">
								<th class="py-1 pr-2">Asset</th>
								<th class="py-1 pr-2">Type</th>
								<th class="py-1 pr-2">Side</th>
								<th class="py-1 pr-2 text-right">Qty</th>
								<th class="py-1 pr-2 text-right">Price / trigger</th>
								<th class="py-1 pr-2">Status</th>
								<th class="py-1 text-right">Actions</th>
							</tr>
						</thead>
						<tbody>
							{#each openOrders as row}
								{@const orderId = pickStr(row, 'orderId', 'order_id', 'id')}
								<tr class="border-b border-[#151515] text-[#999]">
									<td class="py-1 pr-2 font-bold text-[#bbb]">{pickStr(row, 'asset', 'coin')}</td>
									<td class="py-1 pr-2">{pickStr(row, 'type')}</td>
									<td class="py-1 pr-2">{pickStr(row, 'side')}</td>
									<td class="py-1 pr-2 text-right">{fmtNum(pickNum(row, 'quantity'), 5)}</td>
									<td class="py-1 pr-2 text-right">
										{fmtNum(pickNum(row, 'price') ?? pickNum(row, 'triggerPrice', 'trigger_price'))}
									</td>
									<td class="py-1 pr-2 text-[#777]">{pickStr(row, 'status')}</td>
									<td class="py-1 text-right">
										<button
											class="border border-[#333] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[#888] hover:text-white disabled:opacity-50"
											on:click={() => cancelOrder(row)}
											disabled={cancellingOrder === orderId}
										>
											{cancellingOrder === orderId ? 'Cancelling…' : 'Cancel'}
										</button>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>

		<!-- ─────────────────────── recent trades ─────────────────────── -->
		<div class="border border-[#222] bg-[#050505] p-4 space-y-2">
			<h2 class="text-sm font-bold uppercase tracking-wider text-white">Recent trades</h2>
			{#if sectionErrors.trades}
				<div class="text-[11px] text-yellow-500">{sectionErrors.trades}</div>
			{:else if trades.length === 0}
				<div class="text-[11px] text-[#555]">No trades yet.</div>
			{:else}
				<div class="overflow-x-auto">
					<table class="w-full text-[11px]">
						<thead>
							<tr class="text-left text-[10px] uppercase tracking-wider text-[#666] border-b border-[#222]">
								<th class="py-1 pr-2">When</th>
								<th class="py-1 pr-2">Asset</th>
								<th class="py-1 pr-2">Type</th>
								<th class="py-1 pr-2">Side</th>
								<th class="py-1 pr-2 text-right">Qty</th>
								<th class="py-1 pr-2 text-right">Price</th>
								<th class="py-1 pr-2 text-right">Fee</th>
								<th class="py-1 text-right">Realized PnL</th>
							</tr>
						</thead>
						<tbody>
							{#each trades as row}
								{@const rpnl = pickNum(row, 'realizedPnl', 'realized_pnl')}
								<tr class="border-b border-[#151515] text-[#999]">
									<td class="py-1 pr-2 text-[#777]">{fmtWhen(pick(row, 'createdAt', 'created_at', 'timestamp'))}</td>
									<td class="py-1 pr-2 font-bold text-[#bbb]">{pickStr(row, 'asset', 'coin')}</td>
									<td class="py-1 pr-2">{pickStr(row, 'type')}</td>
									<td class="py-1 pr-2">{pickStr(row, 'side')}</td>
									<td class="py-1 pr-2 text-right">{fmtNum(pickNum(row, 'quantity'), 5)}</td>
									<td class="py-1 pr-2 text-right">{fmtNum(pickNum(row, 'price'))}</td>
									<td class="py-1 pr-2 text-right text-[#777]">{fmtNum(pickNum(row, 'fee'), 4)}</td>
									<td class={`py-1 text-right ${(rpnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
										{fmtUsd(rpnl)}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>
	{/if}
</div>
