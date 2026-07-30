import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

export const ssr = false;

// The page renders from the snapshot store client-side; the load function
// only preserves the legacy tab-URL redirects.
export const load: PageLoad = ({ url }) => {
	const view = url.searchParams.get('view');
	if (view === 'quant_factory' || view === 'quant' || view === 'beta' || view === 'spec') {
		throw redirect(301, '/');
	}
};
