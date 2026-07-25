import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { mount, unmount } from 'svelte';

// The factory used to REPLACE the whole module with two stubs, so everything else
// $lib/api/forven exports vanished under the mock — including
// FACTORY_RESET_CONFIRM_PHRASE, the constant that must equal both the phrase the
// operator types and the `Literal["FACTORY RESET"]` the backend accepts. That is why
// SettingsDangerZone.svelte redeclares the phrase locally, and it meant this suite
// could not check the two agree. Spread the real module and stub ONLY the two calls
// that would hit the network: the mock now matches the module's real surface, and the
// phrase assertion below is against the genuine exported constant.
vi.mock('$lib/api/forven', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/forven')>();
	return {
		...actual,
		getFactoryResetCategories: vi.fn(),
		performFactoryReset: vi.fn(),
	};
});

import SettingsDangerZone from '../lib/components/settings/sections/SettingsDangerZone.svelte';
import {
	FACTORY_RESET_CONFIRM_PHRASE,
	getFactoryResetCategories,
	performFactoryReset,
} from '$lib/api/forven';

const mockGet = getFactoryResetCategories as unknown as ReturnType<typeof vi.fn>;
const mockReset = performFactoryReset as unknown as ReturnType<typeof vi.fn>;

let target: HTMLElement;
let instance: any;

async function flush(): Promise<void> {
	await Promise.resolve();
	await Promise.resolve();
	await new Promise((r) => setTimeout(r, 0));
	await Promise.resolve();
}

beforeEach(() => {
	mockGet.mockReset();
	mockReset.mockReset();
	mockGet.mockResolvedValue({
		categories: [
			{ id: 'brain', label: 'Brain memory', description: 'Curated lessons', default_keep: true },
			{ id: 'market_data', label: 'Market data', description: 'OHLCV cache', default_keep: false },
		],
	});
});

afterEach(() => {
	if (instance) unmount(instance);
	target?.remove();
});

describe('SettingsDangerZone factory reset', () => {
	it('renders the factory reset panel with keep options from the catalog', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsDangerZone, { target, props: { settings: {} } });
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('Factory reset');
		expect(text).toContain('Keep Brain memory');
		expect(text).toContain('Keep Market data');
		expect(mockGet).toHaveBeenCalledTimes(1);

		// default_keep drives the initial checkbox state.
		const brain = target.querySelector('#keep-brain') as HTMLInputElement | null;
		const market = target.querySelector('#keep-market_data') as HTMLInputElement | null;
		expect(brain?.checked).toBe(true);
		expect(market?.checked).toBe(false);
	});

	it('requires a typed confirmation before wiping', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsDangerZone, { target, props: { settings: {} } });
		await flush();

		const trigger = Array.from(target.querySelectorAll('button')).find((b) =>
			/factory reset/i.test(b.textContent || ''),
		);
		expect(trigger).toBeTruthy();
		trigger!.click();
		await flush();

		expect(target.textContent).toContain('Confirm factory reset');
		expect(target.querySelector('[role="dialog"]')).not.toBeNull();
		// Nothing is wiped until the operator confirms.
		expect(mockReset).not.toHaveBeenCalled();
	});

	it('arms on the exported confirm phrase and posts the kept category ids', async () => {
		// API-10: the component redeclares the phrase locally so it stays mountable
		// under this mock. Typing the REAL exported constant is what proves the two
		// have not drifted — a rename on either side fails here instead of at runtime,
		// where the backend would 422 an armed-looking button.
		mockReset.mockResolvedValue({ status: 'ok', wiped: ['market_data'], kept: ['brain'] });
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsDangerZone, { target, props: { settings: {} } });
		await flush();

		const trigger = Array.from(target.querySelectorAll('button')).find((b) =>
			/factory reset/i.test(b.textContent || ''),
		);
		trigger!.click();
		await flush();

		const input = target.querySelector('[role="dialog"] input[type="text"]') as HTMLInputElement;
		expect(input).toBeTruthy();
		input.value = FACTORY_RESET_CONFIRM_PHRASE;
		input.dispatchEvent(new Event('input', { bubbles: true }));
		await flush();

		const confirm = Array.from(
			target.querySelectorAll('[role="dialog"] button'),
		).find((b) => /wipe|confirm|reset/i.test(b.textContent || '') && !(b as HTMLButtonElement).disabled);
		expect(confirm, 'confirm button should arm once the exact phrase is typed').toBeTruthy();
		(confirm as HTMLButtonElement).click();
		await flush();

		// default_keep from the catalog: brain kept, market_data wiped.
		expect(mockReset).toHaveBeenCalledWith(['brain']);
	});
});
