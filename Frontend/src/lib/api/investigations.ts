import { cachedGet, invalidate, request } from './client';
import type { EventsResponse, InvestigationSummary, Workspace } from '../types';

const enc = encodeURIComponent;
const base = (id: string) => `/investigations/${enc(id)}`;

export const list = (query: { status?: string; limit?: number; offset?: number } = {}) =>
	cachedGet<{ items: InvestigationSummary[] }>('/investigations', { query }, 20_000);

/** Workspaces change while agents run, so they are always fetched fresh. */
export const workspace = (id: string, signal?: AbortSignal) => request<Workspace>(base(id), { signal });

export const events = (id: string, afterSeq: number, signal?: AbortSignal) =>
	request<EventsResponse>(`${base(id)}/events`, { query: { after_seq: afterSeq }, signal });

function changed() {
	invalidate('/investigations', '/reviews/', '/ews/watchlist', '/ews/portfolio');
}

async function act(path: string, body: unknown, method: 'POST' | 'PUT' = 'POST'): Promise<Workspace> {
	const result = await request<Workspace>(path, { method, body });
	changed();
	return result;
}

export interface InitiateBody {
	scope: 'customer' | 'portfolio';
	customer_id?: string;
	portfolio_segment?: string | null;
	question?: string;
	context?: string;
	context_category?: string;
	requested_specialists?: string[];
}

export const initiate = (body: InitiateBody) => act('/investigations', body);

export const addNote = (id: string, body: { kind: string; content: string; category?: string; document_name?: string }) =>
	act(`${base(id)}/notes`, body);
export const requestTask = (id: string, body: { specialist: string; question?: string; hypothesis_code?: string }) =>
	act(`${base(id)}/tasks`, body);
export const challengeHypothesis = (id: string, hypothesisId: string, body: { statement: string; request_specialist?: string }) =>
	act(`${base(id)}/hypotheses/${enc(hypothesisId)}/challenge`, body);
export const reviewFinding = (id: string, findingId: string, body: { action: string; reason: string }) =>
	act(`${base(id)}/findings/${enc(findingId)}/review`, body);
export const resolveChallenge = (id: string, challengeId: string, body: { action: string; reason: string }) =>
	act(`${base(id)}/challenges/${enc(challengeId)}/resolve`, body);
export const reviewRecord = (id: string, recordId: string, body: { action: string; reason: string }) =>
	act(`${base(id)}/records/${enc(recordId)}/review`, body);
export const validateEvidence = (id: string, body: { outcome: string; reason: string }) =>
	act(`${base(id)}/validate-evidence`, body);
export const requestEvidence = (id: string, body: { description: string }) => act(`${base(id)}/evidence-requests`, body);
export const escalate = (id: string, body: { reason: string }) => act(`${base(id)}/escalate`, body);
export const completeReview = (id: string, body: { reason: string; recommended_classification?: string }) =>
	act(`${base(id)}/complete-review`, body);
export const decideClassification = (
	id: string,
	body: { action: string; reason: string; classification?: string; severity?: string },
) => act(`${base(id)}/classification-decision`, body);
export const decideIntervention = (id: string, interventionId: string, body: { action: string; reason: string }) =>
	act(`${base(id)}/interventions/${enc(interventionId)}/decision`, body);
export const portfolioDecision = (id: string, body: { action: string; reason: string; classification?: string }) =>
	act(`${base(id)}/portfolio-decision`, body);
export const reopen = (id: string, body: { reason: string }) => act(`${base(id)}/reopen`, body);
export const cancel = (id: string, body: { reason: string }) => act(`${base(id)}/cancel`, body);
export const setMonitoring = (
	id: string,
	body: { reopen_score_delta?: number; reopen_if_dpd_over?: number; review_frequency_days?: number; note?: string; reason?: string },
) => act(`${base(id)}/monitoring`, body, 'PUT');
export const evaluateMonitoring = (id: string) => act(`${base(id)}/monitoring/evaluate`, {});
