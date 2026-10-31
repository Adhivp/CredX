import { cachedGet, invalidate, request } from './client';
import type {
	AlertClassification,
	AlertDecision,
	AssessmentRunResult,
	CustomerDetail,
	Explanation,
	PortfolioSummary,
	UploadResult,
	UploadType,
	WatchlistQuery,
	WatchlistResponse,
} from '../types';

const enc = encodeURIComponent;

export const portfolioSummary = () => cachedGet<PortfolioSummary>('/ews/portfolio');

export const watchlist = (query: WatchlistQuery) =>
	cachedGet<WatchlistResponse>('/ews/watchlist', { query: query as Record<string, string | number> });

export const customer = (customerId: string) => cachedGet<CustomerDetail>(`/ews/customer/${enc(customerId)}`);

/** Everything derived from assessments or customer data. */
export function invalidateEws() {
	invalidate('/ews/', '/portfolio/', '/investigations', '/reviews/');
}

export async function runAssessment(customerId?: string): Promise<AssessmentRunResult> {
	const result = await request<AssessmentRunResult>('/ews/assess', {
		method: 'POST',
		query: { customer_id: customerId },
	});
	invalidateEws();
	return result;
}

export async function classifyAlert(assessmentId: string, decision: AlertDecision, reason: string) {
	const result = await request<AlertClassification>(`/ews/alert/${enc(assessmentId)}`, {
		method: 'POST',
		body: { decision, reason },
	});
	invalidate('/ews/customer/');
	return result;
}

export const explain = (customerId: string, signal?: AbortSignal) =>
	request<Explanation>(`/ews/explain/${enc(customerId)}`, { method: 'POST', signal });

export async function upload(dataType: UploadType, file: File): Promise<UploadResult> {
	const form = new FormData();
	form.append('file', file, file.name);
	const result = await request<UploadResult>('/ews/upload', { method: 'POST', query: { data_type: dataType }, form });
	invalidateEws();
	return result;
}
