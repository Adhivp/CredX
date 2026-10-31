/** Review queue, portfolio intelligence, system status and administration. */

import { cachedGet, request } from './client';
import type { AdminUser, AuditEntry, PolicyVersion, PortfolioIntelligence, ReviewQueue, SystemStatus } from '../types';

export const reviewQueue = () => cachedGet<ReviewQueue>('/reviews/queue', {}, 20_000);

export const portfolioIntelligence = (segment?: string) =>
	cachedGet<PortfolioIntelligence>('/portfolio/intelligence', { query: { segment } });

export const systemStatus = () => cachedGet<SystemStatus>('/system/status');

export const users = () => request<{ items: AdminUser[] }>('/admin/users');

export function assignRole(userId: string, body: { role: string; approval_limit?: number | null; reason: string }) {
	return request<AdminUser>(`/admin/users/${encodeURIComponent(userId)}/role`, { method: 'PUT', body });
}

export function grantScope(userId: string, body: { scope_type: string; scope_value: string; reason: string }) {
	return request<AdminUser>(`/admin/users/${encodeURIComponent(userId)}/scopes`, { method: 'POST', body });
}

export function revokeScope(userId: string, body: { scope_type: string; scope_value: string; reason: string }) {
	return request<AdminUser>(`/admin/users/${encodeURIComponent(userId)}/scopes`, { method: 'DELETE', body });
}

export const policies = () => request<{ items: PolicyVersion[] }>('/admin/policies');

export const auditLogs = (query: { resource_id?: string; outcome?: string; limit?: number }) =>
	request<{ items: AuditEntry[] }>('/audit/logs', { query });
