import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';
import {
	getDashboardOverview,
	getDashboardActivity,
	getDashboardWinners,
} from '$lib/api';
import type {
	DashboardOverview,
	DashboardActivityItem,
	WinnerEntry,
} from '$lib/api';

export const ssr = false;

export const load: PageLoad = async ({ url }) => {
	const view = url.searchParams.get('view');
	if (view === 'quant_factory' || view === 'quant' || view === 'beta' || view === 'spec') {
		throw redirect(301, '/');
	}

	const [overview, activity, winners] = await Promise.allSettled([
		getDashboardOverview(),
		getDashboardActivity(40),
		getDashboardWinners(10),
	]);

	return {
		overview: overview.status === 'fulfilled' ? overview.value : null,
		activity: activity.status === 'fulfilled' ? activity.value : [],
		winners: winners.status === 'fulfilled' ? winners.value : [],
	} satisfies {
		overview: DashboardOverview | null;
		activity: DashboardActivityItem[];
		winners: WinnerEntry[];
	};
};
