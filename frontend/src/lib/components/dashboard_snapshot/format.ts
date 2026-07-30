/** Null-safe formatting helpers for the dashboard preview tiles.
 *
 * Truth rule: unknown is "—", never 0. Every numeric formatter here returns
 * an em dash for null/undefined/non-finite input so a missing metric can
 * never render as a reassuring zero.
 */

export function fmtNum(value: unknown, digits = 0): string {
	if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
	return value.toLocaleString(undefined, {
		minimumFractionDigits: digits,
		maximumFractionDigits: digits,
	});
}

export function fmtUsd(value: unknown, digits = 2): string {
	if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
	return `$${value.toLocaleString(undefined, {
		minimumFractionDigits: digits,
		maximumFractionDigits: digits,
	})}`;
}

export function fmtPct(value: unknown, digits = 1): string {
	if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
	return `${value.toFixed(digits)}%`;
}

export function fmtAge(iso: unknown, now: number): string {
	if (typeof iso !== 'string' || !iso) return '—';
	const parsed = Date.parse(iso.includes('T') ? iso : `${iso.replace(' ', 'T')}Z`);
	if (Number.isNaN(parsed)) return '—';
	return fmtAgeSeconds(Math.max(0, (now - parsed) / 1000));
}

export function fmtAgeSeconds(seconds: unknown): string {
	if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '—';
	if (seconds < 90) return `${Math.round(seconds)}s`;
	if (seconds < 90 * 60) return `${Math.round(seconds / 60)}m`;
	if (seconds < 48 * 3600) return `${(seconds / 3600).toFixed(1)}h`;
	return `${(seconds / 86400).toFixed(1)}d`;
}

export function asRecord(value: unknown): Record<string, unknown> {
	return value && typeof value === 'object' && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: {};
}

export function asList(value: unknown): Record<string, unknown>[] {
	return Array.isArray(value)
		? value.filter((entry): entry is Record<string, unknown> => Boolean(entry) && typeof entry === 'object')
		: [];
}
