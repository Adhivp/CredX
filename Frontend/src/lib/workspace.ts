/**
 * Investigation workspace: renders a backend workspace (GET /investigations/{id}) and
 * runs every human action through the API. Nothing here changes state locally; each
 * action re-renders from the workspace the API returns. While agents run, the event
 * stream is polled and the workspace is refetched when new events arrive.
 */

import * as api from './api/investigations';
import { customer as fetchCustomer } from './api/ews';
import { isAbort } from './api/client';
import { count, duration, epochDateTime, humanize, isoDate, money, moneyCompact, pct, relative, score, shortId } from './format';
import { html, mount, type Html } from './html';
import {
	AGENTS,
	CLASSIFICATION,
	CONTEXT_CATEGORY,
	EVENT_LABEL,
	INVESTIGATION_STATUS,
	QUEUE,
	SEVERITY,
	agentName,
	origin,
	requester,
	textOf,
	toneOf,
	type Tone,
} from './labels';
import type { PageContext } from './page';
import { can, roleLabel, type Permission } from './permissions';
import { CLASSIFICATIONS, CONTEXT_CATEGORIES, SEVERITIES, SPECIALISTS, type Finding, type InvestigationEvent, type Workspace } from './types';
import { emptyState, errorState, failureMessage, openDialog, pill, statusPill, summary, toast, type Field } from './ui';

const ACTIVE = new Set(['investigating', 'awaiting_analyst_review', 'awaiting_approval', 'incomplete']);
const REVIEWABLE = new Set([...ACTIVE, 'monitoring', 'approved', 'rejected']);
const MONITORED = new Set(['monitoring', 'approved', 'rejected']);
const RUNNING_STATUSES = new Set(['created', 'investigating', 'reopened']);
const PREPARER_ACTIONS = new Set(['initiate', 'request_investigation', 'complete_analyst_review', 'escalate', 'challenge_hypothesis']);
const QUIET_EVENTS = new Set(['node_entered', 'wave_started']);
const RECORD_TABLES = new Set(['repayment_records', 'cashflow_records', 'customer_exposures', 'loan_facilities']);

const DIRECTION: Record<string, { text: string; tone: Tone }> = {
	supports: { text: 'Supports hypothesis', tone: 'serious' },
	contradicts: { text: 'Contradicts hypothesis', tone: 'good' },
	inconclusive: { text: 'Inconclusive', tone: 'neutral' },
	neutral: { text: 'Neutral', tone: 'neutral' },
};

const HYPOTHESIS_STATUS: Record<string, { text: string; tone: Tone }> = {
	open: { text: 'Open', tone: 'info' },
	active: { text: 'Active', tone: 'info' },
	supported: { text: 'Supported', tone: 'serious' },
	refuted: { text: 'Refuted', tone: 'good' },
	deferred: { text: 'Deferred', tone: 'neutral' },
	inconclusive: { text: 'Inconclusive', tone: 'warning' },
};

const CHALLENGE_STATUS: Record<string, { text: string; tone: Tone }> = {
	open: { text: 'Open', tone: 'warning' },
	unresolved: { text: 'Unresolved', tone: 'warning' },
	escalated: { text: 'Escalated to a human', tone: 'serious' },
	addressed: { text: 'Addressed', tone: 'good' },
	dismissed: { text: 'Dismissed', tone: 'neutral' },
	upheld: { text: 'Upheld', tone: 'serious' },
};

const TASK_STATUS: Record<string, { text: string; tone: Tone }> = {
	pending: { text: 'Pending', tone: 'neutral' },
	requested: { text: 'Requested', tone: 'neutral' },
	authorised: { text: 'Authorised', tone: 'info' },
	running: { text: 'Running', tone: 'info' },
	completed: { text: 'Completed', tone: 'good' },
	failed: { text: 'Failed', tone: 'critical' },
	timed_out: { text: 'Timed out', tone: 'critical' },
	rejected: { text: 'Rejected', tone: 'neutral' },
	cancelled: { text: 'Cancelled', tone: 'neutral' },
	deferred: { text: 'Deferred', tone: 'neutral' },
};

const FINDING_ACTION: Record<string, string> = {
	accept_finding: 'Accepted by a reviewer',
	reject_finding: 'Rejected by a reviewer',
	flag_unsupported: 'Flagged as unsupported',
};

interface Options {
	mode: 'full' | 'review';
}

interface View {
	ws: Workspace;
	timeline: 'key' | 'all';
	names: Record<string, string>;
	durations: Record<string, number>;
}

function names(ws: Workspace): Record<string, string> {
	const out: Record<string, string> = {};
	for (const d of ws.decisions) if (d.user?.id && d.user.name) out[d.user.id] = d.user.name;
	return out;
}

function durations(ws: Workspace): Record<string, number> {
	const out: Record<string, number> = {};
	for (const e of ws.events) {
		if (e.type === 'task_completed' && e.task_id && typeof e.payload.duration_ms === 'number') out[e.task_id] = e.payload.duration_ms as number;
	}
	return out;
}

const classificationPill = (c: string | null | undefined) => (c ? pill(textOf(CLASSIFICATION, c), toneOf(CLASSIFICATION, c)) : pill('Not adjudicated', 'neutral'));

// ── Summary ──────────────────────────────────────────────────────────────

function head(v: View, opts: Options): Html {
	const ws = v.ws;
	const initiator = ws.decisions.find((d) => d.action === 'initiate');
	const subject = ws.scope === 'customer' ? ws.customer_id : `Portfolio: ${ws.segment ? humanize(ws.segment) : 'all segments'}`;
	const running = ws.run_state === 'running' || RUNNING_STATUSES.has(ws.status);
	return html`<header class="ws-head">
		<div class="ws-head__main">
			<h1 class="page-head__title page-head__title--sm">${opts.mode === 'review' ? html`<a href="/investigation?id=${encodeURIComponent(ws.id)}">${subject}</a>` : subject}</h1>
			<div class="ws-head__status">${statusPill(ws.status)}${running ? pill('Agents running', 'info') : ''}<span class="muted">Case ${shortId(ws.id)}</span></div>
			<dl class="ws-facts">
				<div><dt>Scope</dt><dd>${ws.scope === 'customer' ? 'Customer' : 'Portfolio'}${ws.scope === 'customer' && ws.customer_id ? html` <a href="/customer?id=${encodeURIComponent(ws.customer_id)}">profile</a>` : ''}</dd></div>
				<div><dt>Initiated by</dt><dd>${initiator?.user.name ?? (ws.trigger === 'analyst_request' ? 'An analyst' : humanize(ws.trigger))}</dd></div>
				<div><dt>Started</dt><dd>${epochDateTime(ws.created_at)}</dd></div>
				<div><dt>Round</dt><dd class="tnum">${ws.round}</dd></div>
				<div><dt>Pathway</dt><dd>${ws.pathway ?? '—'}</dd></div>
				<div><dt>Trigger</dt><dd>${humanize(ws.trigger)}</dd></div>
				<div><dt>Policy</dt><dd>${ws.policy ? `Version ${ws.policy.version}` : '—'}</dd></div>
				<div><dt>Updated</dt><dd>${relative(ws.updated_at)}</dd></div>
			</dl>
			${ws.question ? html`<p class="ws-question"><span class="muted">Question:</span> ${ws.question}</p>` : ''}
		</div>
		${classificationCard(ws)}
	</header>`;
}

function classificationCard(ws: Workspace): Html {
	const eff = ws.effective_classification;
	const adj = ws.adjudications[ws.adjudications.length - 1];
	if (!eff || !adj) {
		return html`<aside class="panel panel--ink ws-class"><p class="ws-class__label">Classification</p><p class="ws-class__value">Not adjudicated yet</p>
			<p class="note">The credit risk adjudicator classifies the case once specialists have reported.</p></aside>`;
	}
	const source =
		eff.source === 'human_override'
			? 'Human override; the adjudicator’s original classification is kept below'
			: eff.source === 'human_accepted'
				? 'Adjudicator classification, accepted by an authorised manager'
				: 'Adjudicator classification, awaiting or without a human decision';
	return html`<aside class="panel panel--ink ws-class" aria-label="Classification">
		<p class="ws-class__label">Effective classification</p>
		<p class="ws-class__value">${textOf(CLASSIFICATION, eff.classification)}</p>
		<div class="ws-class__pills">${pill(textOf(SEVERITY, eff.severity), toneOf(SEVERITY, eff.severity))}</div>
		<dl class="facts ws-class__facts">
			<div><dt>Risk score</dt><dd class="tnum">${score(adj.risk_score)}</dd></div>
			<div><dt>Confidence</dt><dd class="tnum">${pct(adj.confidence)}</dd></div>
			<div><dt>Exposure at risk</dt><dd class="tnum">${moneyCompact(adj.exposure_at_risk)}</dd></div>
			<div><dt>Evidence</dt><dd>${humanize(adj.completeness)}</dd></div>
		</dl>
		<p class="note">${source}.</p>
	</aside>`;
}

// ── Actions ──────────────────────────────────────────────────────────────

interface Action {
	id: string;
	label: string;
	needs: Permission;
	when: (ws: Workspace) => boolean;
	primary?: boolean;
	danger?: boolean;
}

const ACTIONS: Action[] = [
	{ id: 'complete-review', label: 'Complete analyst review', needs: 'complete_analyst_review', when: (w) => w.scope === 'customer' && ['awaiting_analyst_review', 'incomplete'].includes(w.status), primary: true },
	{ id: 'escalate', label: 'Escalate for approval', needs: 'escalate', when: (w) => w.scope === 'customer' && ['awaiting_analyst_review', 'incomplete'].includes(w.status) && w.adjudications.length > 0 },
	{ id: 'classification', label: 'Decide classification', needs: 'decide_classification', when: (w) => w.status === 'awaiting_approval', primary: true },
	{ id: 'portfolio-decision', label: 'Decide portfolio assessment', needs: 'decide_portfolio', when: (w) => w.scope === 'portfolio' && w.status === 'awaiting_analyst_review', primary: true },
	{ id: 'request-task', label: 'Request specialist analysis', needs: 'request_investigation', when: (w) => ACTIVE.has(w.status) || w.status === 'created' },
	{ id: 'validate', label: 'Validate evidence', needs: 'validate_evidence', when: (w) => ACTIVE.has(w.status) },
	{ id: 'evidence-request', label: 'Request more evidence', needs: 'validate_evidence', when: (w) => REVIEWABLE.has(w.status) },
	{ id: 'note', label: 'Add context', needs: 'add_context', when: (w) => REVIEWABLE.has(w.status) || w.status === 'created' },
	{ id: 'monitoring', label: 'Monitoring instructions', needs: 'set_monitoring', when: (w) => REVIEWABLE.has(w.status) },
	{ id: 'evaluate', label: 'Evaluate monitoring now', needs: 'set_monitoring', when: (w) => MONITORED.has(w.status) },
	{ id: 'reopen', label: 'Reopen investigation', needs: 'reopen_investigation', when: (w) => MONITORED.has(w.status), primary: true },
	{ id: 'cancel', label: 'Cancel investigation', needs: 'cancel_investigation', when: (w) => ACTIVE.has(w.status) || ['created', 'reopened'].includes(w.status), danger: true },
];

function preparedByMe(ws: Workspace, userId: string) {
	return ws.decisions.some((d) => d.user.id === userId && PREPARER_ACTIONS.has(d.action));
}

function actionBar(v: View, ctx: PageContext): Html {
	const ws = v.ws;
	const role = ctx.user.role;
	const available = ACTIONS.filter((a) => can(role, a.needs) && a.when(ws));
	const openReview = ws.reviews.find((r) => r.status === 'open');
	const fourEyes = ws.status === 'awaiting_approval' && can(role, 'decide_classification') && preparedByMe(ws, ctx.user.id);
	return html`<section class="ws-actions" aria-label="Actions">
		<div class="ws-actions__text">
			${openReview
				? html`<p class="ws-actions__title">${QUEUE[openReview.queue] ?? humanize(openReview.queue)} is open</p>
					<p class="note">Requires a ${roleLabel(openReview.required_role).toLowerCase()}. ${openReview.reasons.length ? `Reasons: ${openReview.reasons.join('; ')}.` : ''}</p>`
				: html`<p class="ws-actions__title">${textOf(INVESTIGATION_STATUS, ws.status)}</p><p class="note">No human review is open for this case.</p>`}
			${fourEyes ? html`<p class="callout callout--warning">You prepared this case, so the four-eyes rule in the active policy prevents you from approving it. Another manager must decide.</p>` : ''}
		</div>
		<div class="btn-row">${available.length
			? available.map((a) => html`<button type="button" class="btn ${a.primary ? 'btn--primary' : a.danger ? 'btn--ghost ws-danger' : 'btn--secondary'} btn--sm" data-ws="${a.id}">${a.label}</button>`)
			: html`<span class="note">Your role has no actions at this stage.</span>`}</div>
	</section>`;
}

// ── Adjudication & interventions ─────────────────────────────────────────

function adjudication(v: View, ctx: PageContext): Html {
	const ws = v.ws;
	if (!ws.adjudications.length) return html`<h2 class="panel__title">Adjudication</h2>${emptyState('Not adjudicated yet', 'The adjudicator records a classification after the specialists report.')}`;
	const latest = ws.adjudications[ws.adjudications.length - 1];
	const decidedInterventions = new Map(ws.decisions.filter((d) => d.target_type === 'intervention').map((d) => [d.target_id, d]));
	const canDecide = can(ctx.user.role, 'decide_intervention') && ws.status === 'awaiting_approval';
	const override = ws.decisions.filter((d) => d.target_id === latest.id && ['override_classification', 'override_portfolio_assessment', 'accept_classification', 'accept_portfolio_assessment'].includes(d.action));
	return html`<div class="panel__head"><h2 class="panel__title">Adjudication</h2><span class="panel__meta">Round ${latest.round}, scoring ${latest.scoring_version}, ${epochDateTime(latest.created_at)}</span></div>
		<div class="ws-adj">
			<div class="ws-adj__row">${classificationPill(latest.classification)}${pill(textOf(SEVERITY, latest.severity), toneOf(SEVERITY, latest.severity))}<span class="muted">Recommendation: ${humanize(latest.recommendation)}</span></div>
			${override.map((d) => html`<p class="callout ${d.action.startsWith('override') ? 'callout--warning' : 'callout--good'}">${d.user.name ?? 'A manager'} (${roleLabel(d.role)}) ${d.action.startsWith('override') ? `overrode this to ${textOf(CLASSIFICATION, String(d.new_state.classification ?? ''))}${d.new_state.severity ? `, ${textOf(SEVERITY, String(d.new_state.severity))}` : ''}` : 'accepted this classification'}: “${d.reason}”. The adjudicator’s output above is unchanged.</p>`)}
			${latest.rationale.length ? html`<h3 class="ws-sub">Rationale</h3><ul class="signal-list">${latest.rationale.map((r) => html`<li>${r}</li>`)}</ul>` : ''}
			${latest.escalation_reasons.length ? html`<h3 class="ws-sub">Why a human is needed</h3><ul class="signal-list">${latest.escalation_reasons.map((r) => html`<li>${r}</li>`)}</ul>` : ''}
			${latest.proposed_interventions.length
				? html`<h3 class="ws-sub">Proposed interventions</h3>
				<ul class="list ws-interventions">${latest.proposed_interventions.map((i) => {
					const d = decidedInterventions.get(i.id);
					return html`<li class="row"><span class="row__main"><span class="row__title">${humanize(i.code)}</span>
						<span class="row__sub">${i.requires_approval ? 'Requires authorised approval' : 'No approval required'}${d ? `. ${d.action === 'approve_intervention' ? 'Approved' : 'Rejected'} by ${d.user.name ?? 'a manager'}: ${d.reason}` : ''}</span></span>
						${d ? pill(d.action === 'approve_intervention' ? 'Approved, not executed' : 'Rejected', d.action === 'approve_intervention' ? 'good' : 'neutral') : i.requires_approval && canDecide
							? html`<span class="btn-row"><button type="button" class="btn btn--secondary btn--sm" data-ws="intervention" data-id="${i.id}" data-code="${i.code}" data-verdict="approve_intervention">Approve</button><button type="button" class="btn btn--ghost btn--sm" data-ws="intervention" data-id="${i.id}" data-code="${i.code}" data-verdict="reject_intervention">Reject</button></span>`
							: ''}</li>`;
				})}</ul>
				<p class="note">CredX records the authorisation only. Executing an approved intervention belongs to a separately authorised downstream process and is never triggered from here.</p>`
				: ''}
			${ws.adjudications.length > 1
				? html`<details class="disclosure ws-prev" data-key="adj-prev"><summary>Earlier rounds (${ws.adjudications.length - 1})</summary><ul class="list">${ws.adjudications
						.slice(0, -1)
						.reverse()
						.map((a) => html`<li class="row"><span class="row__main"><span class="row__title">Round ${a.round}: ${textOf(CLASSIFICATION, a.classification)}</span><span class="row__sub">${textOf(SEVERITY, a.severity)}, score ${score(a.risk_score)}, ${epochDateTime(a.created_at)}</span></span></li>`)}</ul></details>`
				: ''}
		</div>`;
}

// ── Findings ─────────────────────────────────────────────────────────────

function evidenceTable(f: Finding, v: View, ctx: PageContext): Html {
	if (!f.evidence.length) return html`<p class="note">No source records were cited for this finding.</p>`;
	const canReview = v.ws.scope === 'customer' && can(ctx.user.role, 'validate_evidence') && ACTIVE.has(v.ws.status);
	const reviewed = new Map(v.ws.decisions.filter((d) => d.target_type === 'record').map((d) => [d.target_id, d]));
	return html`<div class="table-wrap"><table class="table table--compact ws-evidence"><thead><tr><th>Source</th><th>Record</th><th>Field</th><th>Observed value</th><th><span class="sr-only">Actions</span></th></tr></thead>
		<tbody>${f.evidence.map((e) => {
			const r = reviewed.get(e.record_id);
			return html`<tr><td>${humanize(e.source_table)}</td><td class="tnum">${shortId(e.record_id)}</td><td>${humanize(e.field)}</td><td class="tnum">${e.observed_value}</td>
				<td class="ws-evidence__acts">${e.source_table === 'repayment_records' && v.ws.customer_id ? html`<button type="button" class="btn btn--link" data-ws="open-record" data-record="${e.record_id}">Open record</button>` : ''}
				${r ? pill(r.action === 'dispute_evidence' ? 'Disputed' : 'Confirmed', r.action === 'dispute_evidence' ? 'warning' : 'good', r.reason) : canReview && RECORD_TABLES.has(e.source_table)
					? html`<button type="button" class="btn btn--link" data-ws="record" data-record="${e.record_id}" data-verdict="confirm_evidence">Confirm</button><button type="button" class="btn btn--link" data-ws="record" data-record="${e.record_id}" data-verdict="dispute_evidence">Dispute</button>`
					: ''}</td></tr>`;
		})}</tbody></table></div>`;
}

function findingCard(f: Finding, v: View, ctx: PageContext): Html {
	const canReview = can(ctx.user.role, 'review_finding') && REVIEWABLE.has(v.ws.status) && !f.human_review;
	const hypothesis = v.ws.hypotheses.find((h) => h.id === f.hypothesis_id);
	const dir = DIRECTION[f.direction] ?? { text: humanize(f.direction), tone: 'neutral' as Tone };
	return html`<article class="finding ${f.excluded_from_adjudication ? 'is-excluded' : ''}">
		<header class="finding__head">
			<div><p class="finding__agent">${agentName(f.agent)}, round ${f.round}</p><h3 class="finding__code">${humanize(f.code)}</h3></div>
			<div class="finding__meta">${pill(dir.text, dir.tone)}<span class="tnum muted">Confidence ${pct(f.confidence)}</span></div>
		</header>
		${hypothesis ? html`<p class="note">Hypothesis: ${hypothesis.statement}</p>` : ''}
		<p class="finding__summary">${f.summary}</p>
		${f.interpretation ? html`<p class="finding__interp"><span class="muted">${f.interpretation_source === 'llm' ? 'Agent narrative (model-written, figures checked)' : 'Interpretation'}:</span> ${f.interpretation}</p>` : ''}
		${f.uncertainty ? html`<p class="note">Uncertainty: ${f.uncertainty}</p>` : ''}
		${f.contradictions.length ? html`<div class="callout callout--warning"><strong>Contradictions.</strong> ${f.contradictions.map((c) => (typeof c === 'string' ? c : JSON.stringify(c))).join('; ')}</div>` : ''}
		${f.missing_evidence.length ? html`<div class="callout"><strong>Missing evidence.</strong> ${f.missing_evidence.map((c) => (typeof c === 'string' ? c : JSON.stringify(c))).join('; ')}</div>` : ''}
		${f.human_review ? html`<p class="callout ${f.human_review.action === 'accept_finding' ? 'callout--good' : 'callout--warning'}">${FINDING_ACTION[f.human_review.action] ?? humanize(f.human_review.action)}${v.names[f.human_review.user_id] ? ` (${v.names[f.human_review.user_id]})` : ''}${f.human_review.reason ? `: ${f.human_review.reason}` : ''}.${f.excluded_from_adjudication ? ' Excluded from adjudication; the original finding is kept.' : ''}</p>` : ''}
		<details class="disclosure finding__more" data-key="ev-${f.id}"><summary>Evidence (${f.evidence.length} records)</summary>${evidenceTable(f, v, ctx)}</details>
		${f.calculations.length
			? html`<details class="disclosure finding__more" data-key="calc-${f.id}"><summary>Calculations (${f.calculations.map((c) => `${c.name} v${c.version}`).join(', ')})</summary>
				${f.calculations.map((c) => html`<div class="calc"><p class="note">${c.name} v${c.version}, ${count(c.input_record_count)} input records, input digest ${c.input_digest.slice(0, 16)}</p><pre class="code">${JSON.stringify(c.output, null, 2)}</pre></div>`)}</details>`
			: ''}
		${Object.keys(f.metrics).length ? html`<details class="disclosure finding__more" data-key="met-${f.id}"><summary>Metrics</summary><pre class="code">${JSON.stringify(f.metrics, null, 2)}</pre></details>` : ''}
		${canReview
			? html`<div class="btn-row finding__acts"><button type="button" class="btn btn--secondary btn--sm" data-ws="finding" data-id="${f.id}" data-verdict="accept_finding">Accept</button>
				<button type="button" class="btn btn--ghost btn--sm" data-ws="finding" data-id="${f.id}" data-verdict="reject_finding">Reject</button>
				<button type="button" class="btn btn--ghost btn--sm" data-ws="finding" data-id="${f.id}" data-verdict="flag_unsupported">Flag unsupported</button></div>`
			: ''}
	</article>`;
}

function findings(v: View, ctx: PageContext): Html {
	const list = v.ws.findings;
	return html`<div class="panel__head"><h2 class="panel__title">Findings</h2><span class="panel__meta">${count(list.length)} from ${count(new Set(list.map((f) => f.agent)).size)} specialists</span></div>
		${list.length ? html`<div class="findings">${[...list].reverse().map((f) => findingCard(f, v, ctx))}</div>` : emptyState('No findings yet', v.ws.run_state === 'running' ? 'Specialists are working; findings appear as they are recorded.' : 'Specialists record findings with cited evidence.')}`;
}

// ── Hypotheses & challenges ──────────────────────────────────────────────

function hypotheses(v: View, ctx: PageContext): Html {
	const canChallenge = can(ctx.user.role, 'challenge_hypothesis') && ACTIVE.has(v.ws.status);
	return html`<div class="panel__head"><h2 class="panel__title">Hypotheses</h2></div>
		${v.ws.hypotheses.length
			? html`<ul class="list">${[...v.ws.hypotheses].sort((a, b) => b.priority - a.priority).map(
					(h) => html`<li><div class="row"><span class="row__main"><span class="row__title">${humanize(h.code)}</span></span>${pill(textOf(HYPOTHESIS_STATUS, h.status), toneOf(HYPOTHESIS_STATUS, h.status))}</div>
						<p class="ws-text">${h.statement}</p>
						<p class="note">Raised by ${origin(h.source, v.names)} in round ${h.round_created}${h.rationale ? `. ${h.rationale}` : ''}</p>
						${canChallenge ? html`<button type="button" class="btn btn--link" data-ws="challenge" data-id="${h.id}" data-code="${h.code}">Challenge this hypothesis</button>` : ''}</li>`,
				)}</ul>`
			: emptyState('No hypotheses yet')}`;
}

function challenges(v: View, ctx: PageContext): Html {
	const canResolve = can(ctx.user.role, 'resolve_challenge') && ACTIVE.has(v.ws.status);
	return html`<div class="panel__head"><h2 class="panel__title">Challenges</h2><span class="panel__meta">Raised by the evidence challenger or a reviewer</span></div>
		${v.ws.challenges.length
			? html`<ul class="list">${v.ws.challenges.map((c) => {
					const target = c.target_type === 'hypothesis' ? v.ws.hypotheses.find((h) => h.id === c.target_id)?.code : c.target_type === 'finding' ? v.ws.findings.find((f) => f.id === c.target_id)?.code : null;
					const open = ['open', 'unresolved', 'escalated'].includes(c.status);
					return html`<li><div class="row"><span class="row__main"><span class="row__title">${humanize(c.type)}</span><span class="row__sub">${humanize(c.materiality)} materiality, round ${c.round}${target ? `, on ${humanize(target)}` : ''}</span></span>${pill(textOf(CHALLENGE_STATUS, c.status), toneOf(CHALLENGE_STATUS, c.status))}</div>
						<p class="ws-text">${c.statement}</p>
						<p class="note">Raised by ${origin(c.raised_by, v.names)}${c.followup_task_ids.length ? `; ${c.followup_task_ids.length} follow-up task${c.followup_task_ids.length === 1 ? '' : 's'}` : ''}${c.resolution ? `. ${c.resolution}` : ''}</p>
						${canResolve && open ? html`<span class="btn-row"><button type="button" class="btn btn--link" data-ws="resolve" data-id="${c.id}" data-verdict="uphold">Uphold</button><button type="button" class="btn btn--link" data-ws="resolve" data-id="${c.id}" data-verdict="dismiss">Dismiss</button></span>` : ''}</li>`;
				})}</ul>`
			: emptyState('No challenges raised')}`;
}

// ── Agent activity ───────────────────────────────────────────────────────

function tasks(v: View): Html {
	const list = v.ws.tasks;
	return html`<div class="panel__head"><h2 class="panel__title">Agent activity</h2><span class="panel__meta">${count(list.length)} tasks</span></div>
		${list.length
			? html`<ol class="tasks">${list.map((t) => {
					const ms = v.durations[t.id];
					return html`<li class="task"><div class="row"><span class="row__main"><span class="row__title">${agentName(t.agent)}</span>
						<span class="row__sub">Round ${t.round}, wave ${t.wave}${t.attempts > 1 ? `, ${t.attempts} attempts` : ''}</span></span>${pill(textOf(TASK_STATUS, t.status), toneOf(TASK_STATUS, t.status))}</div>
						<p class="ws-text">${t.question}</p>
						<p class="note">${requester(t.requested_by, v.names)}${t.parent_task_id ? ', follow-up of an earlier task' : ''}${ms !== undefined ? `. Ran ${ms < 1000 ? `${Math.round(ms)} ms` : duration(ms / 1000)}` : t.started_at && t.completed_at ? `. Ran ${duration(t.completed_at - t.started_at)}` : ''}${t.completion_status ? `, ${humanize(t.completion_status).toLowerCase()}` : ''}</p>
						${t.error ? html`<p class="callout callout--critical">${t.error}</p>` : ''}</li>`;
				})}</ol>`
			: emptyState('No agent tasks yet')}`;
}

function usage(v: View): Html {
	const rows = v.ws.llm_usage;
	if (!rows.length) return html`<p class="note">No language-model calls were made; all findings are deterministic.</p>`;
	const ok = rows.find((r) => r.status === 'ok');
	return html`<p class="note">Language-model calls: ${rows.map((r) => `${count(r.calls)} ${humanize(r.status).toLowerCase()}`).join(', ')}${ok ? `; ${count(ok.prompt_tokens + ok.completion_tokens)} tokens` : ''}.
		${ok ? '' : 'Agent narrative was unavailable, so findings and adjudication are deterministic only.'}</p>`;
}

// ── People ───────────────────────────────────────────────────────────────

function decisions(v: View): Html {
	const list = v.ws.decisions;
	return html`<div class="panel__head"><h2 class="panel__title">Human decisions</h2><span class="panel__meta">Append-only record</span></div>
		${list.length
			? html`<ol class="list ws-decisions">${[...list].reverse().map(
					(d) => html`<li><div class="row"><span class="row__title">${humanize(d.action)}</span><span class="row__sub">${epochDateTime(d.created_at)}</span></div>
						<p class="note">${d.user.name ?? 'A user'}, ${roleLabel(d.role)}${d.target_type !== 'investigation' ? `, on ${humanize(d.target_type)}` : ''}</p>
						${d.reason ? html`<p class="ws-text">${d.reason}</p>` : ''}</li>`,
				)}</ol>`
			: emptyState('No human decisions yet')}`;
}

function notes(v: View): Html {
	const list = v.ws.notes;
	return html`<div class="panel__head"><h2 class="panel__title">Analyst context</h2></div>
		${list.length
			? html`<ul class="list">${[...list].reverse().map(
					(n) => html`<li><div class="row"><span class="row__title">${humanize(n.kind)}${n.category ? `: ${CONTEXT_CATEGORY[n.category] ?? humanize(n.category)}` : ''}</span><span class="row__sub">${relative(n.created_at)}</span></div>
						<p class="ws-text">${n.content}</p><p class="note">${n.user ?? 'A user'}, ${roleLabel(n.role)}${n.document_name ? `, document ${n.document_name}` : ''}</p></li>`,
				)}</ul>`
			: html`<p class="note">No context added. Context is shared with agents as untrusted information, never as an instruction.</p>`}`;
}

function reviews(v: View): Html {
	const list = v.ws.reviews;
	return html`<div class="panel__head"><h2 class="panel__title">Review stages</h2></div>
		${list.length
			? html`<ul class="list">${list.map(
					(r) => html`<li><div class="row"><span class="row__main"><span class="row__title">${QUEUE[r.queue] ?? humanize(r.queue)}</span><span class="row__sub">${humanize(r.stage)}, for a ${roleLabel(r.required_role).toLowerCase()}</span></span>${pill(humanize(r.status), r.status === 'open' ? 'warning' : r.status === 'completed' ? 'good' : 'neutral')}</div>
						<p class="note">Opened ${epochDateTime(r.created_at)}${r.completed_at ? `, closed ${epochDateTime(r.completed_at)}${r.completed_by && v.names[r.completed_by] ? ` by ${v.names[r.completed_by]}` : ''}` : ''}</p></li>`,
				)}</ul>`
			: html`<p class="note">No human review has been requested.</p>`}`;
}

function monitoring(v: View): Html {
	const m = v.ws.monitoring_instructions;
	const keys = Object.keys(m);
	return html`<div class="panel__head"><h2 class="panel__title">Monitoring</h2></div>
		${keys.length ? html`<dl class="facts">${keys.map((k) => html`<div><dt>${humanize(k)}</dt><dd>${String(m[k])}</dd></div>`)}</dl>` : html`<p class="note">Policy defaults apply; no case-specific monitoring instructions.</p>`}
		${v.ws.monitoring_events.length ? html`<ul class="list">${[...v.ws.monitoring_events].reverse().map((e) => html`<li class="row"><span class="row__main"><span class="row__title">${humanize(e.decision)}</span><span class="row__sub">${humanize(e.trigger)}, ${epochDateTime(e.created_at)}</span></span></li>`)}</ul>` : ''}`;
}

// ── Timeline ─────────────────────────────────────────────────────────────

function actor(e: InvestigationEvent, v: View): string {
	if (e.actor_type === 'human') return v.names[e.actor_id] ?? 'A user';
	if (e.actor_type === 'agent') return AGENTS[e.actor_id] ?? agentName(e.actor_id);
	return 'System';
}

function eventSummary(e: InvestigationEvent): string {
	const p = e.payload as Record<string, any>;
	switch (e.type) {
		case 'status_changed':
			return `${textOf(INVESTIGATION_STATUS, p.from)} to ${textOf(INVESTIGATION_STATUS, p.to)}${p.reason ? `: ${p.reason}` : ''}`;
		case 'human_decision':
			return `${humanize(p.action)}${p.reason ? `: ${p.reason}` : ''}`;
		case 'plan_created':
			return `Selected ${(p.selected_specialists ?? []).map(agentName).join(', ') || 'no specialists'}`;
		case 'task_started':
		case 'task_authorised':
		case 'delegation_authorised':
		case 'delegation_rejected':
			return [p.agent ? agentName(p.agent) : '', p.requested_by ? `requested by ${requester(p.requested_by)}` : '', p.decision ?? ''].filter(Boolean).join(', ');
		case 'task_completed':
			return `${humanize(p.completion_status)}, ${count(p.findings ?? 0)} finding${p.findings === 1 ? '' : 's'}${typeof p.duration_ms === 'number' ? `, ${Math.round(p.duration_ms)} ms` : ''}${p.llm_status ? `, narrative ${humanize(p.llm_status).toLowerCase()}` : ''}`;
		case 'finding_recorded':
			return `${humanize(p.code)}: ${DIRECTION[p.direction]?.text ?? humanize(p.direction)}, confidence ${pct(p.confidence)}`;
		case 'hypothesis_created':
		case 'hypothesis_activated':
			return humanize(p.code);
		case 'hypothesis_status_changed':
			return `${humanize(p.code)}: ${humanize(p.from)} to ${humanize(p.to)}`;
		case 'challenge_raised':
			return humanize(p.type);
		case 'challenge_updated':
			return `${humanize(p.status)}${p.resolution ? `: ${p.resolution}` : ''}`;
		case 'adjudication_recorded':
			return `${textOf(CLASSIFICATION, p.classification)}, ${textOf(SEVERITY, p.severity)}`;
		case 'review_requested':
			return `${QUEUE[p.queue] ?? humanize(p.queue)}`;
		case 'round_started':
			return `Round ${p.round}${p.reason ? `: ${humanize(p.reason)}` : ''}`;
		case 'run_started':
			return `Trigger: ${humanize(p.trigger)}`;
		case 'node_entered':
			return humanize(p.node);
		case 'completeness_evaluated':
		case 'monitoring_evaluated':
			return `${humanize(p.decision)}${Array.isArray(p.reasons) && p.reasons.length ? `: ${p.reasons.join('; ')}` : ''}`;
		case 'security_flag':
			return humanize(p.type);
		case 'investigation_created':
			return p.question ? `Question: ${p.question}` : `${humanize(p.scope)} investigation`;
		default: {
			const parts = Object.entries(p)
				.filter(([, val]) => ['string', 'number', 'boolean'].includes(typeof val))
				.slice(0, 4)
				.map(([k, val]) => `${humanize(k)}: ${val}`);
			return parts.join(', ');
		}
	}
}

function timeline(v: View): Html {
	const events = v.timeline === 'key' ? v.ws.events.filter((e) => !QUIET_EVENTS.has(e.type)) : v.ws.events;
	return html`<div class="panel__head"><div><h2 class="panel__title">Timeline</h2><span class="panel__meta">${count(v.ws.events.length)} recorded events</span></div>
		<div class="segmented" role="group" aria-label="Events shown"><button type="button" data-ws="timeline" data-verdict="key" aria-pressed="${String(v.timeline === 'key')}">Key events</button><button type="button" data-ws="timeline" data-verdict="all" aria-pressed="${String(v.timeline === 'all')}">All events</button></div></div>
		<ol class="timeline">${[...events].reverse().map(
			(e) => html`<li class="tl tl--${e.actor_type}"><span class="tl__dot" aria-hidden="true"></span>
				<div class="tl__body"><div class="row"><span class="row__title">${EVENT_LABEL[e.type] ?? humanize(e.type)}</span><span class="row__sub tnum">#${e.seq}, ${epochDateTime(e.created_at)}</span></div>
				<p class="tl__text">${eventSummary(e)}</p><p class="note">${actor(e, v)}${e.round ? `, round ${e.round}` : ''}</p></div></li>`,
		)}</ol>`;
}

// ── Dialog flows ─────────────────────────────────────────────────────────

const reasonField = (label = 'Reason', minLength = 5): Field => ({ name: 'reason', label, type: 'textarea', required: true, minLength, maxLength: 2000 });
const classificationOptions = CLASSIFICATIONS.map((c) => ({ value: c, text: textOf(CLASSIFICATION, c) }));

function subjectOf(ws: Workspace) {
	return ws.scope === 'customer' ? ws.customer_id ?? 'customer' : `portfolio ${ws.segment ? humanize(ws.segment) : 'all segments'}`;
}

type Run = (title: string, call: () => Promise<Workspace>, done: string) => Promise<void>;

function flows(v: View, run: Run): Record<string, (data: DOMStringMap) => void> {
	const ws = v.ws;
	const id = ws.id;
	const confirmRows = (rows: [string, string][]) => (vals: Record<string, string>) =>
		summary([['Case', subjectOf(ws)], ...rows, ['Reason', vals.reason ?? '']], 'Recorded permanently with your identity, role and the policy version.');
	return {
		'complete-review': () =>
			openDialog({
				title: 'Complete analyst review',
				description: 'Records your review. If proposed interventions need authorised approval, escalate instead.',
				fields: [{ name: 'recommended_classification', label: 'Recommended classification (optional)', type: 'select', options: [{ value: '', text: 'Keep the adjudicator’s classification' }, ...classificationOptions] }, reasonField()],
				submit: 'Complete review',
				confirm: (vals) => confirmRows([['Recommendation', vals.recommended_classification ? textOf(CLASSIFICATION, vals.recommended_classification) : 'No change']])(vals),
				onSubmit: (vals) => run('complete', () => api.completeReview(id, { reason: vals.reason, recommended_classification: vals.recommended_classification || undefined }), 'Analyst review completed.'),
			}),
		escalate: () =>
			openDialog({
				title: 'Escalate for authorised approval',
				description: 'Sends the case to a credit manager’s approval queue.',
				fields: [reasonField()],
				submit: 'Escalate',
				confirm: confirmRows([['Action', 'Escalate to authorised approval']]),
				onSubmit: (vals) => run('escalate', () => api.escalate(id, { reason: vals.reason }), 'Escalated to authorised approval.'),
			}),
		classification: () =>
			openDialog({
				title: 'Decide the classification',
				description: 'Accept the adjudicator’s classification or override it. An override is recorded separately; the adjudicator’s output is never changed.',
				fields: [
					{ name: 'action', label: 'Decision', type: 'radio', options: [{ value: 'accept_classification', text: 'Accept', hint: textOf(CLASSIFICATION, ws.effective_classification?.classification) }, { value: 'override_classification', text: 'Override' }] },
					{ name: 'classification', label: 'Override classification', type: 'select', options: [{ value: '', text: 'Keep classification' }, ...classificationOptions], hint: 'Used only for an override.' },
					{ name: 'severity', label: 'Override severity', type: 'select', options: [{ value: '', text: 'Keep severity' }, ...SEVERITIES.map((s) => ({ value: s, text: textOf(SEVERITY, s) }))] },
					reasonField(),
				],
				submit: 'Record decision',
				confirm: (vals) =>
					confirmRows([
						['Decision', vals.action === 'accept_classification' ? 'Accept classification' : 'Override classification'],
						...(vals.action === 'override_classification'
							? ([['New classification', vals.classification ? textOf(CLASSIFICATION, vals.classification) : 'Unchanged'], ['New severity', vals.severity ? textOf(SEVERITY, vals.severity) : 'Unchanged']] as [string, string][])
							: []),
					])(vals),
				onSubmit: (vals) =>
					run(
						'classification',
						() =>
							api.decideClassification(id, {
								action: vals.action,
								reason: vals.reason,
								classification: vals.action === 'override_classification' ? vals.classification || undefined : undefined,
								severity: vals.action === 'override_classification' ? vals.severity || undefined : undefined,
							}),
						'Classification decision recorded.',
					),
			}),
		intervention: (d) =>
			openDialog({
				title: `${d.verdict === 'approve_intervention' ? 'Approve' : 'Reject'} ${humanize(d.code)}`,
				description: d.verdict === 'approve_intervention' ? 'Approval is recorded only. CredX does not execute interventions.' : undefined,
				fields: [reasonField()],
				submit: d.verdict === 'approve_intervention' ? 'Approve intervention' : 'Reject intervention',
				danger: d.verdict !== 'approve_intervention',
				confirm: confirmRows([['Intervention', humanize(d.code)], ['Decision', d.verdict === 'approve_intervention' ? 'Approve (recorded, not executed)' : 'Reject']]),
				onSubmit: (vals) => run('intervention', () => api.decideIntervention(id, d.id!, { action: d.verdict!, reason: vals.reason }), 'Intervention decision recorded.'),
			}),
		'portfolio-decision': () =>
			openDialog({
				title: 'Decide the portfolio assessment',
				fields: [
					{ name: 'action', label: 'Decision', type: 'radio', options: [{ value: 'accept_portfolio_assessment', text: 'Accept' }, { value: 'override_portfolio_assessment', text: 'Override' }] },
					{ name: 'classification', label: 'Override classification (optional)', maxLength: 32, placeholder: 'e.g. portfolio_stable' },
					reasonField(),
				],
				submit: 'Record decision',
				confirm: (vals) => confirmRows([['Decision', humanize(vals.action)]])(vals),
				onSubmit: (vals) => run('portfolio', () => api.portfolioDecision(id, { action: vals.action, reason: vals.reason, classification: vals.classification || undefined }), 'Portfolio decision recorded.'),
			}),
		'request-task': () =>
			openDialog({
				title: 'Request specialist analysis',
				description: 'Adds a task for one specialist. The investigation director still authorises it against budget and duplicates.',
				fields: [
					{ name: 'specialist', label: 'Specialist', type: 'select', options: SPECIALISTS.map((s) => ({ value: s, text: agentName(s) })) },
					{ name: 'hypothesis_code', label: 'Hypothesis (optional)', type: 'select', options: [{ value: '', text: 'Specialist default' }, ...ws.hypotheses.map((h) => ({ value: h.code, text: humanize(h.code) }))] },
					{ name: 'question', label: 'Question (optional)', type: 'textarea', maxLength: 600 },
				],
				submit: 'Request analysis',
				onSubmit: (vals) => run('task', () => api.requestTask(id, { specialist: vals.specialist, hypothesis_code: vals.hypothesis_code || undefined, question: vals.question || undefined }), 'Specialist analysis requested.'),
			}),
		validate: () =>
			openDialog({
				title: 'Validate evidence',
				fields: [
					{ name: 'outcome', label: 'Outcome', type: 'radio', options: [{ value: 'complete', text: 'Evidence is complete' }, { value: 'incomplete', text: 'Evidence is incomplete' }] },
					{ name: 'reason', label: 'Reason', type: 'textarea', maxLength: 2000 },
				],
				submit: 'Record validation',
				onSubmit: (vals) => run('validate', () => api.validateEvidence(id, { outcome: vals.outcome, reason: vals.reason }), 'Evidence validation recorded.'),
			}),
		'evidence-request': () =>
			openDialog({
				title: 'Request more evidence',
				fields: [{ name: 'description', label: 'What is missing', type: 'textarea', required: true, minLength: 5, maxLength: 1000 }],
				submit: 'Request evidence',
				onSubmit: (vals) => run('evidence', () => api.requestEvidence(id, { description: vals.description }), 'Evidence request recorded.'),
			}),
		note: () =>
			openDialog({
				title: 'Add context',
				description: 'Context is given to agents as untrusted information and never treated as an instruction.',
				fields: [
					{ name: 'kind', label: 'Kind', type: 'select', options: ['observation', 'context', 'document', 'correction'].map((k) => ({ value: k, text: humanize(k) })) },
					{ name: 'category', label: 'Category', type: 'select', options: [{ value: '', text: 'None' }, ...CONTEXT_CATEGORIES.map((c) => ({ value: c, text: CONTEXT_CATEGORY[c] }))] },
					{ name: 'content', label: 'Content', type: 'textarea', required: true, minLength: 1, maxLength: 4000 },
					{ name: 'document_name', label: 'Document name (optional)', maxLength: 255 },
				],
				submit: 'Add context',
				onSubmit: (vals) => run('note', () => api.addNote(id, { kind: vals.kind, content: vals.content, category: vals.category || undefined, document_name: vals.document_name || undefined }), 'Context added.'),
			}),
		monitoring: () => {
			const m = ws.monitoring_instructions as Record<string, unknown>;
			openDialog({
				title: 'Monitoring instructions',
				description: 'When new data arrives for a monitored case, these thresholds decide whether it reopens.',
				fields: [
					{ name: 'reopen_score_delta', label: 'Reopen if the risk score rises by (points)', type: 'number', min: 0, max: 100, value: m.reopen_score_delta !== undefined ? String(m.reopen_score_delta) : '' },
					{ name: 'reopen_if_dpd_over', label: 'Reopen if days past due exceed', type: 'number', min: 0, max: 365, step: 1, value: m.reopen_if_dpd_over !== undefined ? String(m.reopen_if_dpd_over) : '' },
					{ name: 'review_frequency_days', label: 'Review every (days)', type: 'number', min: 1, max: 365, step: 1, value: m.review_frequency_days !== undefined ? String(m.review_frequency_days) : '' },
					{ name: 'note', label: 'Note', maxLength: 1000, value: m.note ? String(m.note) : '' },
					{ name: 'reason', label: 'Reason', type: 'textarea', maxLength: 2000 },
				],
				submit: 'Save instructions',
				onSubmit: (vals) => {
					const num = (x: string) => (x === '' ? undefined : Number(x));
					return run(
						'monitoring',
						() => api.setMonitoring(id, { reopen_score_delta: num(vals.reopen_score_delta), reopen_if_dpd_over: num(vals.reopen_if_dpd_over), review_frequency_days: num(vals.review_frequency_days), note: vals.note, reason: vals.reason }),
						'Monitoring instructions saved.',
					);
				},
			});
		},
		evaluate: () =>
			openDialog({
				title: 'Evaluate monitoring now',
				description: 'Re-evaluates the customer’s current data against the monitoring thresholds. The case reopens if they are exceeded.',
				fields: [],
				submit: 'Evaluate',
				onSubmit: () => run('evaluate', () => api.evaluateMonitoring(id), 'Monitoring evaluated.'),
			}),
		reopen: () =>
			openDialog({
				title: 'Reopen investigation',
				description: 'Starts a new investigation round with the current data.',
				fields: [reasonField()],
				submit: 'Reopen',
				confirm: confirmRows([['Action', 'Reopen and re-investigate']]),
				onSubmit: (vals) => run('reopen', () => api.reopen(id, { reason: vals.reason }), 'Investigation reopened.'),
			}),
		cancel: () =>
			openDialog({
				title: 'Cancel investigation',
				description: 'Stops the investigation. Its findings and decisions remain on record.',
				fields: [reasonField()],
				submit: 'Cancel investigation',
				danger: true,
				confirm: confirmRows([['Action', 'Cancel the investigation']]),
				onSubmit: (vals) => run('cancel', () => api.cancel(id, { reason: vals.reason }), 'Investigation cancelled.'),
			}),
		finding: (d) => {
			const f = ws.findings.find((x) => x.id === d.id);
			const accept = d.verdict === 'accept_finding';
			openDialog({
				title: `${accept ? 'Accept' : d.verdict === 'reject_finding' ? 'Reject' : 'Flag'} finding`,
				description: f ? `${agentName(f.agent)}: ${f.summary}` : undefined,
				fields: [accept ? { name: 'reason', label: 'Reason (optional)', type: 'textarea', maxLength: 2000 } : reasonField()],
				submit: accept ? 'Accept finding' : d.verdict === 'reject_finding' ? 'Reject finding' : 'Flag as unsupported',
				confirm: accept ? undefined : confirmRows([['Finding', f ? humanize(f.code) : ''], ['Effect', 'Excluded from adjudication; the original finding is kept']]),
				onSubmit: (vals) => run('finding', () => api.reviewFinding(id, d.id!, { action: d.verdict!, reason: vals.reason ?? '' }), 'Finding review recorded.'),
			});
		},
		resolve: (d) =>
			openDialog({
				title: `${d.verdict === 'uphold' ? 'Uphold' : 'Dismiss'} challenge`,
				fields: [reasonField()],
				submit: d.verdict === 'uphold' ? 'Uphold challenge' : 'Dismiss challenge',
				onSubmit: (vals) => run('resolve', () => api.resolveChallenge(id, d.id!, { action: d.verdict!, reason: vals.reason }), 'Challenge resolution recorded.'),
			}),
		challenge: (d) =>
			openDialog({
				title: `Challenge: ${humanize(d.code)}`,
				fields: [
					{ name: 'statement', label: 'What should be tested', type: 'textarea', required: true, minLength: 5, maxLength: 600 },
					{ name: 'request_specialist', label: 'Ask a specialist to test it (optional)', type: 'select', options: [{ value: '', text: 'No specialist' }, ...SPECIALISTS.map((s) => ({ value: s, text: agentName(s) }))] },
				],
				submit: 'Raise challenge',
				onSubmit: (vals) => run('challenge', () => api.challengeHypothesis(id, d.id!, { statement: vals.statement, request_specialist: vals.request_specialist || undefined }), 'Challenge raised.'),
			}),
		record: (d) =>
			openDialog({
				title: d.verdict === 'confirm_evidence' ? 'Confirm source record' : 'Dispute source record',
				description: d.verdict === 'dispute_evidence' ? 'A disputed record is excluded from this investigation’s calculations. The record itself is not changed.' : 'A confirmed record is trusted even if it looks contradictory.',
				fields: [reasonField()],
				submit: d.verdict === 'confirm_evidence' ? 'Confirm record' : 'Dispute record',
				onSubmit: (vals) => run('record', () => api.reviewRecord(id, d.record!, { action: d.verdict!, reason: vals.reason }), 'Evidence review recorded.'),
			}),
	};
}

async function openRecord(ws: Workspace, recordId: string) {
	const dialog = { title: 'Repayment record', body: html`` };
	try {
		const detail = await fetchCustomer(ws.customer_id!);
		const r = detail.repayments.find((x) => x.id === recordId);
		const loan = r ? detail.loans.find((l) => l.id === r.loan_id) : undefined;
		dialog.body = r
			? summary([
					['Loan', loan?.loan_id ?? '—'],
					['Due date', isoDate(r.due_date)],
					['Paid on', r.payment_date ? isoDate(r.payment_date) : 'Not paid'],
					['Amount due', money(r.amount_due)],
					['Amount paid', money(r.amount_paid)],
					['Days past due', String(r.dpd)],
					['Status', humanize(r.repayment_status)],
				])
			: html`<p class="note">The record is no longer returned for this customer.</p>`;
	} catch (error) {
		dialog.body = errorState(error, false);
	}
	openDialog({ title: dialog.title, description: dialog.body, fields: [], submit: 'Close', onSubmit: async () => {} });
}

// ── Assembly ─────────────────────────────────────────────────────────────

export function createWorkspace(root: HTMLElement, initial: Workspace, ctx: PageContext, opts: Options) {
	const v: View = { ws: initial, timeline: 'key', names: names(initial), durations: durations(initial) };
	let polling = false;

	const paint = () => {
		const open = new Set(Array.from(root.querySelectorAll<HTMLDetailsElement>('details[data-key][open]')).map((d) => d.dataset.key));
		const crumb = document.getElementById('ws-crumb');
		if (crumb && opts.mode === 'full') {
			crumb.textContent = subjectOf(v.ws);
			document.title = `${subjectOf(v.ws)} investigation · CredX`;
		}
		const running = v.ws.run_state === 'running' || RUNNING_STATUSES.has(v.ws.status);
		const live = running ? html`<p class="callout callout--info ws-live" role="status">Agents are working. This page follows the investigation's event stream and updates as events are recorded.</p>` : '';
		if (opts.mode === 'review') {
			mount(
				root,
				html`${head(v, opts)}${live}${actionBar(v, ctx)}
				<div class="ws-review">
					<section class="panel">${adjudication(v, ctx)}</section>
					<section class="panel">${findings(v, ctx)}</section>
					<section class="panel">${challenges(v, ctx)}</section>
					<section class="panel">${decisions(v)}</section>
				</div>
				<p class="note"><a href="/investigation?id=${encodeURIComponent(v.ws.id)}">Open the full workspace</a> for agent activity, hypotheses and the complete timeline.</p>`,
			);
		} else {
			mount(
				root,
				html`${head(v, opts)}${live}${actionBar(v, ctx)}
				<div class="ws-grid">
					<div class="ws-col">
						<section class="panel">${adjudication(v, ctx)}</section>
						<section class="panel">${findings(v, ctx)}</section>
						<section class="panel">${hypotheses(v, ctx)}</section>
						<section class="panel">${challenges(v, ctx)}</section>
					</div>
					<div class="ws-col">
						<section class="panel">${tasks(v)}${usage(v)}</section>
						<section class="panel">${reviews(v)}</section>
						<section class="panel">${decisions(v)}</section>
						<section class="panel">${notes(v)}</section>
						<section class="panel">${monitoring(v)}</section>
					</div>
					<section class="panel ws-timeline">${timeline(v)}</section>
				</div>`,
			);
		}
		root.querySelectorAll<HTMLDetailsElement>('details[data-key]').forEach((d) => {
			if (open.has(d.dataset.key)) d.open = true;
		});
	};

	const update = (ws: Workspace) => {
		v.ws = ws;
		v.names = names(ws);
		v.durations = durations(ws);
		paint();
		if (v.ws.run_state === 'running' || RUNNING_STATUSES.has(v.ws.status)) void poll();
	};

	// Poll the event stream only while agents can still be working. Back off when quiet and
	// stop after ten minutes without events.
	async function poll() {
		if (polling) return;
		polling = true;
		let lastSeq = v.ws.events.reduce((m, e) => Math.max(m, e.seq), 0);
		let quietSince = Date.now();
		try {
			while (!ctx.signal.aborted && (v.ws.run_state === 'running' || RUNNING_STATUSES.has(v.ws.status))) {
				const quiet = Date.now() - quietSince;
				if (quiet > 10 * 60_000) {
					toast('No new investigation events for ten minutes; live updates paused. Reload to check again.', 'info');
					break;
				}
				await new Promise((r) => setTimeout(r, quiet > 2 * 60_000 ? 10_000 : 2_000));
				if (ctx.signal.aborted) break;
				const ev = await api.events(v.ws.id, lastSeq, ctx.signal);
				if (ev.events.length || ev.status !== v.ws.status || ev.run_state !== v.ws.run_state) {
					lastSeq = ev.events.reduce((m, e) => Math.max(m, e.seq), lastSeq);
					quietSince = Date.now();
					v.ws = await api.workspace(v.ws.id, ctx.signal);
					v.names = names(v.ws);
					v.durations = durations(v.ws);
					paint();
				}
			}
		} catch (error) {
			if (!isAbort(error)) toast(`Live updates stopped: ${failureMessage(error)}`, 'critical');
		} finally {
			polling = false;
		}
	}

	const run: Run = async (_name, call, done) => {
		const ws = await call(); // errors surface in the dialog; nothing changes locally on failure
		toast(done);
		update(ws);
		document.dispatchEvent(new CustomEvent('credx:workspace-changed', { detail: ws }));
	};

	root.addEventListener('click', (e) => {
		const el = (e.target as HTMLElement).closest<HTMLElement>('[data-ws]');
		if (!el || !root.contains(el)) return;
		const action = el.dataset.ws!;
		if (action === 'timeline') {
			v.timeline = el.dataset.verdict === 'all' ? 'all' : 'key';
			paint();
			return;
		}
		if (action === 'open-record') {
			void openRecord(v.ws, el.dataset.record!);
			return;
		}
		flows(v, run)[action]?.(el.dataset);
	});

	paint();
	if (v.ws.run_state === 'running' || RUNNING_STATUSES.has(v.ws.status)) void poll();
	return { update };
}
