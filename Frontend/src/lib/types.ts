/**
 * Response and request shapes of the CredX API, transcribed from the backend
 * (backend/app/schemas/*, backend/app/services/read_models.py and the route modules).
 * Fields the backend does not return are deliberately absent.
 */

export type Epoch = number; // seconds since the Unix epoch

// ── Authentication ───────────────────────────────────────────────────────

export interface TokenResponse {
	access_token: string;
	refresh_token: string;
	token_type: string;
	expires_in: number;
}

export interface User {
	id: string;
	email: string;
	full_name: string;
	is_active: boolean;
	created_at: Epoch;
	role: string;
}

// ── EWS ──────────────────────────────────────────────────────────────────

export type RiskCategory = 'Low' | 'Medium' | 'High' | 'Critical' | 'Insufficient Data';
export const RISK_CATEGORIES: RiskCategory[] = ['Low', 'Medium', 'High', 'Critical', 'Insufficient Data'];

export interface PortfolioSummary {
	total_customers: number;
	total_assessed: number;
	average_warning_score: number;
	risk_distribution: Record<RiskCategory, number>;
	top_risky_customers: {
		customer_id: string;
		warning_score: number;
		risk_category: RiskCategory;
		top_indicators: string[];
	}[];
}

export interface WatchlistEntry {
	customer_id: string;
	product_type: string;
	/** Outstanding balance of the customer's largest facility, not total exposure. */
	outstanding_exposure: number;
	warning_score: number;
	risk_category: RiskCategory;
	top_indicators: string[];
	active_signals: number;
	monitoring_status: string;
}

export interface WatchlistResponse {
	items: WatchlistEntry[];
	total: number;
	filters: {
		product_type: string | null;
		customer_segment: string | null;
		risk_level: string | null;
		min_score: number;
		max_results: number;
	};
}

export interface WatchlistQuery {
	risk_level?: string;
	product_type?: string;
	customer_segment?: string;
	min_score?: number;
	limit?: number;
	offset?: number;
}

export interface CustomerOut {
	id: string;
	customer_id: string;
	customer_type: string;
	employment_category: string;
	portfolio_segment: string;
	created_at: Epoch;
}

export interface LoanFacility {
	id: string;
	customer_id: string;
	loan_id: string;
	product_type: string;
	sanctioned_amount: number;
	outstanding_balance: number;
	tenure_months: number;
	instalment_amount: number;
	disbursement_date: string;
	status: string;
}

export interface RepaymentRecord {
	id: string;
	loan_id: string; // internal loan row id
	due_date: string;
	payment_date: string | null;
	amount_due: number;
	amount_paid: number;
	dpd: number;
	repayment_status: string;
}

export interface WarningAssessment {
	id: string;
	customer_id: string;
	warning_score: number;
	risk_category: RiskCategory;
	rule_delay_score: number;
	rule_miss_score: number;
	rule_exposure_score: number;
	rule_completeness_score: number;
	cashflow_score: number;
	top_indicators: string[];
	assessed_at: Epoch;
}

export const ALERT_DECISIONS = [
	'Continue Monitoring',
	'Requires Investigation',
	'False Positive',
	'Escalate for Review',
] as const;
export type AlertDecision = (typeof ALERT_DECISIONS)[number];

export interface AlertClassification {
	id: string;
	assessment_id: string;
	decision: AlertDecision;
	reason: string;
	analyst_id: string | null;
	original_score: number;
	original_category: RiskCategory;
	preserved_evidence: {
		top_indicators?: string[];
		rule_scores?: Record<string, number>;
	};
	created_at: Epoch;
}

export interface CustomerDetail {
	customer: CustomerOut;
	loans: LoanFacility[];
	repayments: RepaymentRecord[];
	assessments: WarningAssessment[];
	alerts: AlertClassification[];
}

export interface AssessmentRunResult {
	total_assessed: number;
	scoring_version: string;
	assessments: {
		customer_id: string;
		warning_score: number;
		risk_category: RiskCategory;
		classification: string;
		confidence: number;
		top_indicators: string[];
	}[];
}

export interface Explanation {
	customer_id: string;
	explanation: string;
	contributing_factors: { signal: string; score: number }[];
}

export const UPLOAD_TYPES = ['customers', 'loans', 'repayments', 'exposure', 'cashflow'] as const;
export type UploadType = (typeof UPLOAD_TYPES)[number];

export interface UploadResult {
	upload_id: string;
	filename: string;
	records_loaded: number;
	status: 'success' | 'partial' | 'error';
	errors: string[];
}

// ── Investigations ───────────────────────────────────────────────────────

export interface EffectiveClassification {
	classification: string | null;
	severity: string;
	source: 'adjudicator' | 'human_override' | 'human_accepted';
	adjudication_id: string;
	decision_id?: string;
}

export interface InvestigationSummary {
	id: string;
	scope: 'customer' | 'portfolio';
	customer_id: string | null;
	segment: string | null;
	status: string;
	run_state: string;
	round: number;
	pathway: string | null;
	outcome: string | null;
	trigger: string;
	assigned_analyst_id: string | null;
	effective_classification: EffectiveClassification | null;
	created_at: Epoch;
	updated_at: Epoch;
}

export interface Calculation {
	id: string;
	name: string;
	version: string;
	input_digest: string;
	output: Record<string, unknown>;
	input_record_count: number;
}

export interface EvidenceRef {
	source_table: string;
	record_id: string;
	field: string;
	observed_value: string;
}

export interface Hypothesis {
	id: string;
	code: string;
	statement: string;
	status: string;
	priority: number;
	source: string;
	rationale: string;
	round_created: number;
}

export interface AgentTask {
	id: string;
	agent: string;
	hypothesis_id: string | null;
	question: string;
	status: string;
	requested_by: string;
	parent_task_id: string | null;
	delegation_depth: number;
	round: number;
	wave: number;
	attempts: number;
	completion_status: string | null;
	error: string | null;
	started_at: Epoch | null;
	completed_at: Epoch | null;
}

export interface Finding {
	id: string;
	task_id: string;
	agent: string;
	hypothesis_id: string | null;
	round: number;
	code: string;
	summary: string;
	direction: string;
	confidence: number;
	uncertainty: string;
	contradictions: unknown[];
	missing_evidence: unknown[];
	metrics: Record<string, unknown>;
	calculations: Calculation[];
	evidence: EvidenceRef[];
	interpretation: string | null;
	interpretation_source: string;
	human_review: { action: string; reason: string; user_id: string; decision_id: string } | null;
	excluded_from_adjudication: boolean;
}

export interface Challenge {
	id: string;
	round: number;
	type: string;
	target_type: string;
	target_id: string;
	statement: string;
	materiality: string;
	status: string;
	raised_by: string;
	followup_task_ids: string[];
	resolution: string;
}

export interface Intervention {
	id: string;
	code: string;
	requires_approval: boolean;
	[key: string]: unknown;
}

export interface Adjudication {
	id: string;
	round: number;
	classification: string;
	severity: string;
	risk_score: number | null;
	confidence: number;
	recommendation: string;
	proposed_interventions: Intervention[];
	rationale: string[];
	pathway: string;
	escalation_reasons: string[];
	completeness: string;
	exposure_at_risk: number | null;
	scoring_version: string;
	calculation: Calculation | null;
	created_at: Epoch;
}

export interface ReviewItemRecord {
	id: string;
	queue: string;
	required_role: string;
	stage: string;
	status: string;
	reasons: string[];
	assigned_to: string | null;
	adjudication_id: string | null;
	created_at: Epoch;
	completed_at: Epoch | null;
	completed_by: string | null;
}

export interface HumanDecision {
	id: string;
	user: { id: string; name: string | null; role?: string };
	role: string;
	action: string;
	target_type: string;
	target_id: string;
	previous_state: Record<string, unknown>;
	new_state: Record<string, unknown>;
	reason: string;
	evidence_refs: unknown[];
	policy_version_id: string;
	created_at: Epoch;
}

export interface HumanNote {
	id: string;
	kind: string;
	category: string | null;
	content: string;
	document_name: string | null;
	role: string;
	user: string | null;
	created_at: Epoch;
}

export interface InvestigationEvent {
	seq: number;
	type: string;
	actor_type: string;
	actor_id: string;
	round: number;
	task_id: string | null;
	payload: Record<string, unknown>;
	created_at: Epoch;
}

export interface Workspace extends InvestigationSummary {
	question: string;
	initiated_by: string;
	policy: { id: string; version: number } | null;
	monitoring_instructions: Record<string, unknown>;
	usage: Record<string, unknown>;
	customer: {
		customer_id: string;
		customer_type: string;
		segment: string;
		employment_category: string;
		facilities: {
			loan_id: string;
			product_type: string;
			sanctioned_amount: number;
			outstanding_balance: number;
			instalment_amount: number;
			status: string;
		}[];
	} | null;
	hypotheses: Hypothesis[];
	tasks: AgentTask[];
	findings: Finding[];
	challenges: Challenge[];
	adjudications: Adjudication[];
	reviews: ReviewItemRecord[];
	decisions: HumanDecision[];
	notes: HumanNote[];
	events: InvestigationEvent[];
	llm_usage: { status: string; calls: number; prompt_tokens: number; completion_tokens: number; cost_usd: number }[];
	monitoring_events: { id: string; trigger: string; decision: string; evaluation: Record<string, unknown>; created_at: Epoch }[];
}

export interface EventsResponse {
	status: string;
	run_state: string;
	events: InvestigationEvent[];
}

export const SPECIALISTS = [
	'repayment_specialist',
	'cashflow_specialist',
	'exposure_specialist',
	'baseline_specialist',
	'integrity_specialist',
	'portfolio_specialist',
] as const;

export const CONTEXT_CATEGORIES = [
	'temporary_disruption',
	'seasonal_business',
	'income_change',
	'dispute_or_error',
	'new_obligation',
	'other',
] as const;

export const CLASSIFICATIONS = [
	'low_risk',
	'emerging_deterioration',
	'sustained_deterioration',
	'accelerating_deterioration',
	'recovery',
	'insufficient_evidence',
] as const;

export const SEVERITIES = ['low', 'moderate', 'high', 'severe'] as const;

// ── Reviews ──────────────────────────────────────────────────────────────

export interface ReviewQueueItem {
	review_item_id: string;
	queue: string;
	stage: string;
	required_role: string;
	reasons: string[];
	assigned_to_me: boolean;
	assigned_to: string | null;
	created_at: Epoch;
	investigation: InvestigationSummary;
}

export interface ReviewQueue {
	role: string;
	queues: string[];
	items: ReviewQueueItem[];
}

// ── Portfolio intelligence ───────────────────────────────────────────────

export interface PortfolioGroup {
	group: string;
	suppressed: boolean;
	reason?: string;
	customers?: number;
	deteriorating?: number;
	deteriorating_share?: number;
	insufficient_evidence?: number;
	exposure?: number;
	deteriorating_exposure?: number;
	exposure_share?: number;
	mean_score?: number | null;
	cash_declining_share?: number;
	cluster?: boolean;
}

export interface PortfolioIntelligence {
	calculation: { name: string; version: string; input_digest: string };
	customers: number;
	suppressed_below: number;
	portfolio_deteriorating_share: number;
	total_exposure: number;
	segments: PortfolioGroup[];
	products: PortfolioGroup[];
	employment: PortfolioGroup[];
	segment_exposure_hhi: number;
	clusters: string[];
}

// ── System & administration ──────────────────────────────────────────────

export interface SystemStatus {
	deterministic_engine: { status: string; scoring_version: string };
	llm: {
		provider: string;
		status: string;
		model: string;
		last_successful_call_at: Epoch | null;
		call_status_counts: Record<string, number>;
		note: string;
	};
	ml: {
		status: string;
		used_in_scoring: boolean;
		summary: string;
		findings: string[];
		required_before_use: string[];
		artifacts_present: boolean;
	};
	policy: { id: string; version: number; name: string };
	user: { id: string; role: string };
}

export interface AdminUser {
	id: string;
	email: string;
	full_name: string;
	role: string;
	approval_limit: number | null;
	is_active: boolean;
	scopes: { scope_type: string; scope_value: string }[];
}

export interface PolicyVersion {
	id: string;
	version: number;
	is_active: boolean;
	config: Record<string, unknown>;
	created_by: string | null;
	change_reason: string | null;
	created_at: Epoch;
}

export interface AuditEntry {
	id: string;
	user_id: string | null;
	role: string | null;
	action: string;
	resource_type: string;
	resource_id: string;
	outcome: string;
	detail: Record<string, unknown>;
	created_at: Epoch;
}
