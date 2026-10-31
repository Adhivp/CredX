/**
 * Mirror of ROLE_PERMISSIONS in backend/app/core/rbac.py, used only to decide what to
 * show. The backend enforces every permission and data scope independently; a control
 * hidden here is a courtesy, never a security boundary. GET /users/me returns the role
 * but not its permissions, so this table must be kept in step with the backend.
 */

export type Permission =
	| 'view_customer'
	| 'upload_data'
	| 'run_assessment'
	| 'view_investigation'
	| 'initiate_investigation'
	| 'add_context'
	| 'request_investigation'
	| 'challenge_hypothesis'
	| 'review_finding'
	| 'resolve_challenge'
	| 'validate_evidence'
	| 'complete_analyst_review'
	| 'escalate'
	| 'assign_analyst'
	| 'decide_classification'
	| 'decide_intervention'
	| 'reopen_investigation'
	| 'cancel_investigation'
	| 'set_monitoring'
	| 'view_portfolio'
	| 'initiate_portfolio_investigation'
	| 'decide_portfolio'
	| 'view_review_queue'
	| 'manage_policy'
	| 'manage_users'
	| 'view_audit';

const OFFICER: Permission[] = [
	'view_customer',
	'view_investigation',
	'initiate_investigation',
	'add_context',
	'request_investigation',
	'view_review_queue',
];
const ANALYST: Permission[] = [
	...OFFICER,
	'upload_data',
	'run_assessment',
	'challenge_hypothesis',
	'review_finding',
	'resolve_challenge',
	'validate_evidence',
	'complete_analyst_review',
	'escalate',
	'reopen_investigation',
	'cancel_investigation',
	'set_monitoring',
	'view_portfolio',
];
const MANAGER: Permission[] = [...ANALYST, 'assign_analyst', 'decide_classification', 'decide_intervention'];
const PORTFOLIO: Permission[] = [
	'view_customer',
	'view_investigation',
	'view_portfolio',
	'initiate_portfolio_investigation',
	'decide_portfolio',
	'add_context',
	'request_investigation',
	'view_review_queue',
	'reopen_investigation',
];
const ADMIN: Permission[] = ['manage_policy', 'manage_users', 'view_audit', 'view_investigation', 'view_portfolio'];
const AUDITOR: Permission[] = ['view_audit', 'view_investigation'];

const ROLE_PERMISSIONS: Record<string, ReadonlySet<Permission>> = {
	unassigned: new Set(),
	relationship_officer: new Set(OFFICER),
	credit_analyst: new Set(ANALYST),
	credit_manager: new Set(MANAGER),
	portfolio_manager: new Set(PORTFOLIO),
	risk_admin: new Set(ADMIN),
	auditor: new Set(AUDITOR),
};

export const ROLES = Object.keys(ROLE_PERMISSIONS);

export const ROLE_LABEL: Record<string, string> = {
	unassigned: 'No role assigned',
	relationship_officer: 'Relationship officer',
	credit_analyst: 'Credit risk analyst',
	credit_manager: 'Credit manager',
	portfolio_manager: 'Portfolio risk manager',
	risk_admin: 'Risk administrator',
	auditor: 'Auditor',
};

export const PERMISSION_LABEL: Record<Permission, string> = {
	view_customer: 'View customers and the watchlist',
	upload_data: 'Upload portfolio data',
	run_assessment: 'Run warning assessments',
	view_investigation: 'View investigations',
	initiate_investigation: 'Start customer investigations',
	add_context: 'Add context to investigations',
	request_investigation: 'Request specialist analysis',
	challenge_hypothesis: 'Challenge hypotheses',
	review_finding: 'Review agent findings',
	resolve_challenge: 'Resolve evidence challenges',
	validate_evidence: 'Validate and dispute evidence',
	complete_analyst_review: 'Complete analyst reviews and classify alerts',
	escalate: 'Escalate for authorised approval',
	assign_analyst: 'Assign analysts',
	decide_classification: 'Decide classifications',
	decide_intervention: 'Approve or reject interventions',
	reopen_investigation: 'Reopen investigations',
	cancel_investigation: 'Cancel investigations',
	set_monitoring: 'Set monitoring instructions',
	view_portfolio: 'View portfolio intelligence',
	initiate_portfolio_investigation: 'Start portfolio investigations',
	decide_portfolio: 'Decide portfolio assessments',
	view_review_queue: 'View the review queue',
	manage_policy: 'Publish institutional policy',
	manage_users: 'Manage users, roles and data scopes',
	view_audit: 'View the audit trail and policy history',
};

export function permissionsOf(role: string | undefined): ReadonlySet<Permission> {
	return ROLE_PERMISSIONS[role ?? ''] ?? ROLE_PERMISSIONS.unassigned;
}

export function can(role: string | undefined, permission: Permission): boolean {
	return permissionsOf(role).has(permission);
}

export const roleLabel = (role: string | undefined) => ROLE_LABEL[role ?? ''] ?? role ?? 'Unknown role';

/** Navigation destinations and the permission each needs. Order is the nav order. */
export const NAV: { href: string; label: string; page: string; needs: Permission[] }[] = [
	{ href: '/overview', label: 'Overview', page: 'overview', needs: ['view_customer'] },
	{ href: '/watchlist', label: 'Watchlist', page: 'watchlist', needs: ['view_customer'] },
	{ href: '/investigations', label: 'Investigations', page: 'investigations', needs: ['view_investigation'] },
	{ href: '/portfolio', label: 'Portfolio', page: 'portfolio', needs: ['view_portfolio'] },
	{ href: '/reviews', label: 'Reviews', page: 'reviews', needs: ['view_review_queue'] },
	{ href: '/data', label: 'Data', page: 'data', needs: ['upload_data', 'run_assessment'] },
	{ href: '/admin', label: 'Administration', page: 'admin', needs: ['manage_users', 'view_audit'] },
];

export const navAllowed = (role: string, needs: Permission[]) => needs.some((p) => can(role, p));

/** First page a role can use; the account page is always available. */
export function homeFor(role: string): string {
	return NAV.find((item) => navAllowed(role, item.needs))?.href ?? '/account';
}
