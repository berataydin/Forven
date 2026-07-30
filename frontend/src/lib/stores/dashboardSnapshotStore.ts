/**
 * Client-side snapshot state with last-good retention.
 *
 * The browser-held last-good snapshot is the survival mechanism for backend
 * event-loop stalls (locked plan decision 2): a failed poll NEVER clears the
 * previous snapshot — it only stamps `failedSince` so the UI can overlay an
 * explicit offline/stale indicator on data that stays visible.
 */
import { writable } from 'svelte/store';
import { getDashboardSnapshot, type DashboardSnapshot } from '$lib/api/snapshot';

export interface SnapshotClientState {
	snapshot: DashboardSnapshot | null;
	/** Epoch ms of the last successful client fetch. */
	lastGoodFetchAt: number | null;
	/** Epoch ms of the first failure in the current failure streak, or null. */
	failedSince: number | null;
	consecutiveFailures: number;
}

export const snapshotState = writable<SnapshotClientState>({
	snapshot: null,
	lastGoodFetchAt: null,
	failedSince: null,
	consecutiveFailures: 0,
});

let inFlight = false;

export async function refreshSnapshot(): Promise<void> {
	if (inFlight) return;
	inFlight = true;
	try {
		const snapshot = await getDashboardSnapshot();
		snapshotState.set({
			snapshot,
			lastGoodFetchAt: Date.now(),
			failedSince: null,
			consecutiveFailures: 0,
		});
	} catch {
		snapshotState.update((state) => ({
			...state,
			failedSince: state.failedSince ?? Date.now(),
			consecutiveFailures: state.consecutiveFailures + 1,
		}));
	} finally {
		inFlight = false;
	}
}
