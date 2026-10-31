/** Display vocabulary for backend codes. Unknown codes fall back to humanize(). */

import { humanize } from './format';

export type Tone = 'neutral' | 'info' | 'good' | 'warning' | 'serious' | 'critical';

export const INVESTIGATION_STATUS: Record<string, { text: string; tone: Tone }> = {
	created: { text: 'Created', tone: 'neutral' },
	investigating: { text: 'Investigating', tone: 'info' },
	awaiting_analyst_review: { text: 'Awaiting analyst review', tone: 'warning' },
	awaiting_approval: { text: 'Awaiting approval', tone: 'serious' },
	approved: { text: 'Approved', tone: 'good' },
	rejected: { text: 'Rejected', tone: 'critical' },
	incomplete: { text: 'Incomplete evidence', tone: 'warning' },
	monitoring: { text: 'Monitoring', tone: 'good' },
	reopened: { text: 'Reopened', tone: 'info' },
	cancelled: { text: 'Cancelled', tone: 'neutral' },
	failed: { text: 'Failed', tone: 'critical' },
	not_investigated: { text: 'Not investigated', tone: 'neutral' },
};

/** Statuses the backend treats as still needing work (services/investigations.ACTIVE + queued). */
export const OPEN_STATUSES = new Set([
	'created',
	'investigating',
	'awaiting_analyst_review',
	'awaiting_approval',
	'incomplete',
	'reopened',
]);

export const CLASSIFICATION: Record<string, { text: string; tone: Tone }> = {
	low_risk: { text: 'Low risk', tone: 'good' },
	recovery: { text: 'Recovery', tone: 'good' },
	emerging_deterioration: { text: 'Emerging deterioration', tone: 'warning' },
	sustained_deterioration: { text: 'Sustained deterioration', tone: 'serious' },
	accelerating_deterioration: { text: 'Accelerating deterioration', tone: 'critical' },
	insufficient_evidence: { text: 'Insufficient evidence', tone: 'neutral' },
	portfolio_cluster: { text: 'Portfolio cluster', tone: 'serious' },
	portfolio_stable: { text: 'Portfolio stable', tone: 'good' },
};

export const SEVERITY: Record<string, { text: string; tone: Tone }> = {
	low: { text: 'Low severity', tone: 'good' },
	moderate: { text: 'Moderate severity', tone: 'warning' },
	high: { text: 'High severity', tone: 'serious' },
	severe: { text: 'Severe', tone: 'critical' },
	undetermined: { text: 'Undetermined', tone: 'neutral' },
};

export const AGENTS: Record<string, string> = {
	investigation_director: 'Investigation director',
	repayment_specialist: 'Repayment behaviour specialist',
	cashflow_specialist: 'Cash-flow specialist',
	exposure_specialist: 'Exposure and obligation specialist',
	baseline_specialist: 'Behavioural baseline specialist',
	integrity_specialist: 'Evidence and data integrity specialist',
	portfolio_specialist: 'Portfolio specialist',
	evidence_challenger: 'Evidence challenger',
	credit_risk_adjudicator: 'Credit risk adjudicator',
};

export const agentName = (id: string | null | undefined) => (id ? AGENTS[id] ?? humanize(id) : '—');

export const QUEUE: Record<string, string> = {
	analyst_review: 'Analyst review',
	authorised_approval: 'Authorised approval',
	portfolio_review: 'Portfolio review',
};

export const CONTEXT_CATEGORY: Record<string, string> = {
	temporary_disruption: 'Temporary disruption',
	seasonal_business: 'Seasonal business',
	income_change: 'Income change',
	dispute_or_error: 'Dispute or data error',
	new_obligation: 'New obligation',
	other: 'Other',
};

/**
 * The component scores stored on a WarningAssessment row (0–100). The first four are
 * risk signals (higher is worse) and are the ones the watchlist counts as "active" above
 * 20; completeness runs the other way (higher means a more complete record).
 */
export const SCORE_COMPONENTS: { key: string; label: string; detail: string; risk: boolean }[] = [
	{ key: 'rule_delay_score', label: 'Repayment delay', detail: 'core.rules repayment-delay score (days past due, late payments)', risk: true },
	{ key: 'rule_miss_score', label: 'Missed payments', detail: 'core.rules miss classification score (temporary vs sustained misses)', risk: true },
	{ key: 'rule_exposure_score', label: 'Exposure pressure', detail: 'Exposure family score from the deterministic engine', risk: true },
	{ key: 'cashflow_score', label: 'Cash-flow decline', detail: 'Cash-flow family score from the deterministic engine', risk: true },
	{
		key: 'rule_completeness_score',
		label: 'Repayment history completeness',
		detail: 'core.rules missing-history score; higher means a more complete record',
		risk: false,
	},
];

export const REPAYMENT_STATUS_TONE: Record<string, Tone> = {
	on_time: 'good',
	late: 'warning',
	partial: 'serious',
	missed: 'critical',
};

export const toneOf = (map: Record<string, { tone: Tone }>, key: string | null | undefined): Tone =>
	(key && map[key]?.tone) || 'neutral';
export const textOf = (map: Record<string, { text: string }>, key: string | null | undefined): string =>
	(key && map[key]?.text) || humanize(key);

/** Who asked for something, never presenting a human as an agent or vice versa. */
export function requester(value: string, names: Record<string, string> = {}): string {
	if (value.startsWith('human:')) return `Analyst request${names[value.slice(6)] ? ` (${names[value.slice(6)]})` : ''}`;
	if (value.startsWith('agent:')) return `Delegated by ${agentName(value.slice(6))}`;
	return agentName(value);
}

/** Who raised a hypothesis or challenge. */
export function origin(value: string, names: Record<string, string> = {}): string {
	if (value.startsWith('human:')) return names[value.slice(6)] ?? 'an analyst';
	if (value.startsWith('agent:')) return agentName(value.slice(6));
	return agentName(value);
}

export const EVENT_LABEL: Record<string, string> = {
	investigation_created: 'Investigation created',
	run_started: 'Agent run started',
	node_entered: 'Workflow step',
	evidence_screened: 'Evidence screened',
	hypothesis_created: 'Hypothesis generated',
	hypothesis_activated: 'Hypothesis activated',
	hypothesis_status_changed: 'Hypothesis status changed',
	plan_created: 'Specialists selected',
	round_started: 'Investigation round started',
	wave_started: 'Task wave started',
	task_authorised: 'Task authorised',
	delegation_authorised: 'Delegation authorised',
	delegation_rejected: 'Delegation rejected',
	task_started: 'Task started',
	task_completed: 'Task completed',
	task_retry_scheduled: 'Task retry scheduled',
	finding_recorded: 'Finding recorded',
	challenge_raised: 'Challenge raised',
	challenge_updated: 'Challenge updated',
	challenge_escalated: 'Challenge escalated to a human',
	reinvestigation_budget_exhausted: 'Re-investigation budget exhausted',
	completeness_evaluated: 'Evidence completeness evaluated',
	adjudication_recorded: 'Adjudication completed',
	review_requested: 'Human review requested',
	human_decision: 'Human decision recorded',
	status_changed: 'Status changed',
	monitoring_evaluated: 'Monitoring evaluated',
	security_flag: 'Safeguard triggered',
};
