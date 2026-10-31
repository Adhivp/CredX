/**
 * Shared helpers for the Level 2 monitoring pages (portfolio monitor, priority
 * queue, investigation workspace, interventions).
 */
import { api } from './api';

export const EWS = '/api/v1/ews';

export const esc = (v: unknown): string =>
  String(v ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch] as string));

/** Name recorded on audit entries: the signed-in user, else "analyst". */
export function actorName(): string {
  return api.getUser()?.full_name || 'analyst';
}

export async function ewsFetch<T = any>(path: string, opts: { method?: string; body?: unknown; query?: Record<string, unknown> } = {}): Promise<T> {
  const qs = opts.query
    ? '?' + Object.entries(opts.query).filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`).join('&')
    : '';
  const headers: Record<string, string> = { Accept: 'application/json' };
  const token = api.getAccessToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  let res = await fetch(`${EWS}${path}${qs === '?' ? '' : qs}`, {
    method: opts.method || 'GET', headers, body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && token) {
    // Access tokens are short-lived: refresh once and retry.
    const fresh = await api.refreshToken().catch(() => null);
    if (fresh) {
      headers['Authorization'] = `Bearer ${fresh.access_token}`;
      res = await fetch(`${EWS}${path}${qs === '?' ? '' : qs}`, {
        method: opts.method || 'GET', headers, body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      });
    }
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const d = (err as any).detail;
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map((e: any) => e.msg).join(', ') : `HTTP ${res.status}`);
  }
  return res.json();
}

export function inr(amount: number | null | undefined): string {
  const a = Number(amount || 0);
  if (Math.abs(a) >= 1e7) return `₹${(a / 1e7).toFixed(2)} Cr`;
  if (Math.abs(a) >= 1e5) return `₹${(a / 1e5).toFixed(2)} L`;
  return `₹${Math.round(a).toLocaleString('en-IN')}`;
}

export const pct = (v: number | null | undefined, digits = 0) => (v === null || v === undefined ? '–' : `${(v * 100).toFixed(digits)}%`);
export const title = (s: string) => (s || '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

export function levelBadge(level: string): string {
  if (level === 'Insufficient Data') return `<span class="badge badge-insufficient">Insufficient Data</span>`;
  return `<span class="badge badge-${esc((level || 'low').toLowerCase())}">${esc(level)}</span>`;
}

const TREND_META: Record<string, [string, string, string]> = {
  deteriorating: ['▲', 'Deteriorating', 'trend-bad'],
  improving: ['▼', 'Improving', 'trend-good'],
  stable: ['■', 'Stable', 'trend-flat'],
  insufficient_data: ['?', 'No trend', 'trend-flat'],
  insufficient_history: ['?', 'New', 'trend-flat'],
  no_history: ['?', 'No history', 'trend-flat'],
};

export function trendBadge(direction: string, delta?: number | null): string {
  const [icon, label, cls] = TREND_META[direction] || ['■', title(direction), 'trend-flat'];
  const d = delta === null || delta === undefined ? '' : ` <span class="mono">${delta > 0 ? '+' : ''}${delta.toFixed(1)}</span>`;
  return `<span class="trend-pill ${cls}"><span aria-hidden="true">${icon}</span> ${label}${d}</span>`;
}

export function tierBadge(tier: string, label?: string): string {
  return `<span class="tier-pill tier-${esc((tier || 'p4').toLowerCase())}" title="${esc(label || '')}">${esc(tier)}</span>`;
}

export function dpdBadge(d: { bucket: string; dpd: number; delinquent: boolean }): string {
  if (!d || !d.delinquent) return `<span class="dpd-pill dpd-current">Standard</span>`;
  return `<span class="dpd-pill dpd-late" title="${d.dpd} days past due">${esc(d.bucket)} · ${d.dpd}d</span>`;
}

const STATUS_CLASS: Record<string, string> = {
  planned: 'st-neutral', pending_approval: 'st-warn', approved: 'st-info', initiated: 'st-info',
  completed: 'st-good', deferred: 'st-neutral', escalated: 'st-bad', cancelled: 'st-muted',
  open: 'st-info', under_investigation: 'st-info', awaiting_information: 'st-warn',
  action_in_progress: 'st-info', monitoring: 'st-neutral', closed: 'st-muted',
};

export function statusBadge(status: string): string {
  return `<span class="status-pill ${STATUS_CLASS[status] || 'st-neutral'}">${esc(title(status))}</span>`;
}

export function fmtDate(ts: number | string | null | undefined): string {
  if (!ts) return '–';
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
  return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
}

export function fmtDateTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

/** Shared floating tooltip for SVG charts (one element, positioned on hover). */
let tipEl: HTMLDivElement | null = null;
export function bindTooltips(root: HTMLElement) {
  if (!tipEl) {
    tipEl = document.createElement('div');
    tipEl.className = 'chart-tooltip';
    tipEl.setAttribute('role', 'tooltip');
    document.body.appendChild(tipEl);
  }
  const tip = tipEl;
  root.querySelectorAll<SVGElement | HTMLElement>('[data-tip]').forEach((el) => {
    el.addEventListener('pointerenter', () => { tip.innerHTML = el.getAttribute('data-tip') || ''; tip.style.opacity = '1'; });
    el.addEventListener('pointermove', (e: PointerEvent) => {
      const x = Math.min(window.innerWidth - tip.offsetWidth - 12, e.clientX + 14);
      tip.style.transform = `translate(${x}px, ${e.clientY + 14}px)`;
    });
    el.addEventListener('pointerleave', () => { tip.style.opacity = '0'; });
  });
}

/** Single-series line chart (warning score over time) with per-point hover. */
export function lineChart(points: { label: string; value: number | null; tip?: string }[], opts: { height?: number; max?: number; bands?: boolean } = {}): string {
  const W = 640, H = opts.height || 200, P = { l: 34, r: 12, t: 12, b: 26 };
  const max = opts.max ?? 100;
  const n = points.length;
  if (!n) return '<p class="muted-note">No history yet.</p>';
  const x = (i: number) => P.l + (n === 1 ? (W - P.l - P.r) / 2 : (i * (W - P.l - P.r)) / (n - 1));
  const y = (v: number) => P.t + (1 - v / max) * (H - P.t - P.b);
  const grid = [0, 20, 45, 70, 100].filter((g) => g <= max).map((g) =>
    `<line x1="${P.l}" x2="${W - P.r}" y1="${y(g)}" y2="${y(g)}" class="chart-grid"/><text x="${P.l - 6}" y="${y(g) + 3}" class="chart-axis" text-anchor="end">${g}</text>`).join('');
  const bands = opts.bands === false ? '' : [
    [70, 100, 'band-critical'], [45, 70, 'band-high'], [20, 45, 'band-medium'],
  ].map(([lo, hi, cls]) => `<rect x="${P.l}" width="${W - P.l - P.r}" y="${y(hi as number)}" height="${y(lo as number) - y(hi as number)}" class="${cls}"/>`).join('');
  const valid = points.map((p, i) => ({ ...p, i })).filter((p) => p.value !== null) as { label: string; value: number; tip?: string; i: number }[];
  const path = valid.map((p, k) => `${k ? 'L' : 'M'}${x(p.i).toFixed(1)},${y(p.value).toFixed(1)}`).join(' ');
  const step = Math.max(1, Math.ceil(n / 8));
  const xlabels = points.map((p, i) => (i % step === 0 || i === n - 1 ? `<text x="${x(i)}" y="${H - 6}" class="chart-axis" text-anchor="middle">${esc(p.label)}</text>` : '')).join('');
  const dots = valid.map((p) => `<circle cx="${x(p.i)}" cy="${y(p.value)}" r="4" class="chart-dot"/>`).join('');
  const colW = (W - P.l - P.r) / Math.max(1, n - 1);
  const hits = valid.map((p) => `<rect x="${x(p.i) - colW / 2}" y="${P.t}" width="${colW}" height="${H - P.t - P.b}" fill="transparent" data-tip="${esc(p.tip || `${p.label}: ${p.value.toFixed(1)}`)}"/>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" aria-label="Warning score over time">${bands}${grid}<path d="${path}" class="chart-line"/>${dots}${xlabels}${hits}</svg>`;
}

/** DPD buckets: ordinal single-hue ramp (validated vs the dark surface); Current is neutral. */
export const BUCKETS = ['Standard', 'SMA-0', 'SMA-1', 'SMA-2', 'NPA'];  // RBI SMA classes
export const BUCKET_COLORS: Record<string, string> = {
  'Standard': '#3F3F46', 'SMA-0': '#FED7AA', 'SMA-1': '#FDBA74', 'SMA-2': '#FB923C', 'NPA': '#EA580C',
};

/** Stacked bars: customers per DPD bucket per period, 2px gaps, hover per segment. */
export function stackedBuckets(series: { period: string; customers: number; buckets: Record<string, number> }[]): string {
  const W = 640, H = 220, P = { l: 30, r: 10, t: 10, b: 26 };
  if (!series.length) return '<p class="muted-note">No periods.</p>';
  const max = Math.max(1, ...series.map((s) => s.customers));
  const slot = (W - P.l - P.r) / series.length;
  const bw = Math.min(36, slot * 0.6);
  const yv = (v: number) => (v / max) * (H - P.t - P.b);
  let bars = '';
  series.forEach((s, i) => {
    const cx = P.l + slot * i + slot / 2;
    let top = H - P.b;
    BUCKETS.forEach((b) => {
      const v = s.buckets[b] || 0;
      if (!v) return;
      const h = Math.max(0, yv(v) - 2);
      top -= yv(v);
      bars += `<rect x="${cx - bw / 2}" y="${top + 2}" width="${bw}" height="${h}" rx="2" fill="${BUCKET_COLORS[b]}" data-tip="${esc(`${s.period} · ${b}: ${v} customer${v === 1 ? '' : 's'}`)}"/>`;
    });
    if (i % Math.max(1, Math.ceil(series.length / 8)) === 0 || i === series.length - 1)
      bars += `<text x="${cx}" y="${H - 8}" class="chart-axis" text-anchor="middle">${esc(s.period.slice(2))}</text>`;
  });
  const ticks = [0, Math.ceil(max / 2), max].map((t) => `<line x1="${P.l}" x2="${W - P.r}" y1="${H - P.b - yv(t)}" y2="${H - P.b - yv(t)}" class="chart-grid"/><text x="${P.l - 6}" y="${H - P.b - yv(t) + 3}" class="chart-axis" text-anchor="end">${t}</text>`).join('');
  const legend = BUCKETS.map((b) => `<span class="legend-item"><span class="legend-swatch" style="background:${BUCKET_COLORS[b]}"></span>${b}</span>`).join('');
  return `<div class="chart-legend">${legend}</div><svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" aria-label="Customers by days past due per month">${ticks}${bars}</svg>`;
}

/** Tiny inline score sparkline for tables. */
export function sparkline(values: number[], w = 84, h = 22): string {
  if (values.length < 2) return '';
  const min = Math.min(...values), max = Math.max(...values);
  const span = Math.max(10, max - min);
  const pts = values.map((v, i) => `${((i / (values.length - 1)) * (w - 4) + 2).toFixed(1)},${(h - 2 - ((v - min) / span) * (h - 4)).toFixed(1)}`).join(' ');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
}

export function toast(msg: string, kind: 'ok' | 'err' = 'ok') {
  const el = document.createElement('div');
  el.className = `ews-toast ${kind}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3800);
}
