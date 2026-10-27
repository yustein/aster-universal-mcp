import { App, applyHostStyleVariables } from '@modelcontextprotocol/ext-apps';

type Dict = Record<string, any>;
type View = 'overview' | 'task' | 'fleet' | 'data';
const root = document.getElementById('aster-root')!;
const app = new App({ name: 'Aster', version: '1.0.0' }, { availableDisplayModes: ['inline', 'fullscreen'] });
const state = {
  snapshot: null as Dict | null, detail: null as Dict | null, detailEnvelope: null as Dict | null,
  view: 'overview' as View, filter: 'all', search: '', connected: false,
  refreshing: false, loadingTask: '', selectedId: '', error: '', notice: '',
  detailError: '', sending: false, requestSequence: 0,
};
const icons: Record<string, string[]> = {
  overview: ['M3 3h7v7H3z', 'M14 3h7v7h-7z', 'M3 14h7v7H3z', 'M14 14h7v7h-7z'],
  fleet: ['M12 12a3 3 0 1 0 0-6 3 3 0 0 0 0 6', 'M6 21v-2a6 6 0 0 1 12 0v2', 'M5 7a2 2 0 0 0-2 2v2', 'M19 7a2 2 0 0 1 2 2v2'],
  archive: ['M4 4h16v5H4z', 'M5 9v11h14V9', 'M10 13h4'],
  refresh: ['M20 7v5h-5', 'M4 17v-5h5', 'M6.1 7a7 7 0 0 1 11.6-2L20 8', 'M4 16l2.3 3A7 7 0 0 0 18 17'],
  arrow: ['m9 5 7 7-7 7'],
  back: ['m14 6-6 6 6 6', 'M8 12h13'],
  search: ['M10.5 18a7.5 7.5 0 1 0 0-15 7.5 7.5 0 0 0 0 15', 'm16 16 5 5'],
  check: ['m5 12 4 4L19 6'],
  clock: ['M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20', 'M12 6v6l4 2'],
  alert: ['m12 3 10 18H2L12 3Z', 'M12 9v4', 'M12 17h.01'],
  chat: ['M21 11a8 8 0 0 1-8 8H6l-4 3V11a9 9 0 0 1 19 0Z', 'M7 10h10', 'M7 14h6'],
  activity: ['M2 12h5l3-8 4 16 3-8h5'],
  document: ['M6 2h8l5 5v15H6z', 'M14 2v6h5', 'M9 12h7', 'M9 16h7'],
  lock: ['M5 10h14v11H5z', 'M8 10V6a4 4 0 0 1 8 0v4'],
  info: ['M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20', 'M12 11v6', 'M12 7h.01'],
};

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text?: unknown): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function icon(name: string, className = ''): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor'); svg.setAttribute('stroke-width', '1.6');
  svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('class', `icon ${className}`);
  for (const d of icons[name] || icons.document) { const p = document.createElementNS(svg.namespaceURI, 'path'); p.setAttribute('d', d); svg.append(p); }
  return svg;
}
function button(label: string, action: () => void | Promise<void>, className = 'button', iconName?: string) {
  const node = el('button', className); node.type = 'button';
  if (iconName) node.append(icon(iconName)); node.append(el('span', '', label));
  node.addEventListener('click', action); return node;
}
function obj(value: unknown): Dict { return value && typeof value === 'object' && !Array.isArray(value) ? value as Dict : {}; }
function str(value: unknown, fallback = ''): string { return typeof value === 'string' ? value : typeof value === 'number' ? String(value) : fallback; }
function label(value: unknown): string { return str(value, 'Not reported').replace(/[_-]+/g, ' ').replace(/^\w/, c => c.toUpperCase()); }
function list(value: unknown): Dict[] { return Array.isArray(value) ? value.filter(v => v && typeof v === 'object' && !Array.isArray(v)) : []; }
function hasValue(value: unknown): boolean { return value !== undefined && value !== null && value !== '' && !(Array.isArray(value) && !value.length) && !(typeof value === 'object' && value !== null && !Object.keys(value).length); }
function timestamp(value: unknown): Date | null {
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
  return Number.isFinite(date.getTime()) ? date : null;
}
function exactTime(value: unknown) {
  const date = timestamp(value); if (!date) return 'Not reported';
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Istanbul', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).formatToParts(date);
  const get = (name: string) => parts.find(p => p.type === name)?.value || '';
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}:${get('second')} UTC+3`;
}
function relativeTime(value: unknown) {
  const date = timestamp(value); if (!date) return 'Not reported';
  const seconds = (Date.now() - date.getTime()) / 1000;
  if (seconds < -60) return 'Timestamp ahead of local clock';
  if (seconds < 60) return 'Just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
function timeNode(value: unknown, className = 'time') {
  const date = timestamp(value); const node = el('time', className, relativeTime(value));
  if (date) { node.dateTime = date.toISOString(); node.title = exactTime(value); node.dataset.relativeTime = 'true'; }
  return node;
}
function statusTone(status: unknown) {
  const s = str(status).toLowerCase();
  if (['completed', 'healthy', 'connected', 'ready', 'ok', 'available', 'sender_verified', 'verified'].includes(s)) return 'teal';
  if (['running', 'accepted', 'active', 'worker_reported'].includes(s)) return 'blue';
  if (['blocked', 'uncertain', 'stale', 'degraded', 'pending', 'pending_or_expired'].includes(s)) return 'amber';
  if (['failed', 'error', 'sender_rejected', 'rejected', 'disabled', 'unavailable'].includes(s)) return 'rose';
  return 'muted';
}
function badge(status: unknown, override?: string) {
  const node = el('span', `badge ${statusTone(status)}`); node.append(el('span', 'status-dot'), el('span', '', override || label(status))); return node;
}
function kicker(text: string) { return el('div', 'eyebrow', text); }
function tasks() { return list(state.snapshot?.tasks); }
function agents() { return list(state.snapshot?.agents); }
function taskId(task: Dict) { return str(task.task_id || task.id); }
function taskTitle(task: Dict) { const full = str(task.title || task.instruction, 'Untitled assignment').replace(/\s+/g, ' ').trim(); return full.length > 140 ? `${full.slice(0, 137)}…` : full; }
function taskReceiver(task: Dict) { return str(task.receiver || task.assigned_to || task.agent_id, 'Unassigned'); }
function agentName(id: unknown) { return str(agents().find(a => a.agent_id === id)?.display_name, str(id, 'Not reported')); }
function count(key: string): number | null { const value = obj(state.snapshot?.counts)[key]; return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null; }
function sum(...keys: string[]): number | null { const values = keys.map(count); return values.every(v => v !== null) ? (values as number[]).reduce((a, b) => a + b, 0) : null; }
function source(key: string) { return obj(obj(state.snapshot?.sources)[key]); }
function sourceTime(value: Dict) { return value.checked_at || value.source_checked_at || value.observed_at || value.as_of; }
function makeEmpty(title: string, description: string, iconName = 'document') {
  const node = el('div', 'empty-state'); node.append(icon(iconName), el('h3', '', title), el('p', '', description)); return node;
}
function go(view: View) { state.view = view; state.error = ''; state.notice = ''; render(); }

function sidebar() {
  const side = el('aside', 'sidebar');
  const brand = el('div', 'brand'); brand.append(el('div', 'brand-mark', '✦'), el('span', '', 'aster')); side.append(brand);
  side.append(el('div', 'sidebar-label', 'WORKSPACE'));
  const nav = el('nav', 'navigation'); nav.setAttribute('aria-label', 'Workspace');
  for (const [view, title, glyph] of [['overview', 'Overview', 'overview'], ['fleet', 'Fleet', 'fleet'], ['data', 'Archive & costs', 'archive']] as const) {
    const active = state.view === view || (state.view === 'task' && view === 'overview');
    const item = button(title, () => go(view), `nav-item${active ? ' active' : ''}`, glyph);
    item.dataset.focusKey = `nav-${view}`; if (active) item.setAttribute('aria-current', 'page'); nav.append(item);
  }
  side.append(nav);
  const foot = el('div', 'sidebar-foot'); foot.append(el('span', 'scope-dot'), el('div', '', 'Personal workspace'), el('small', '', 'Aster Coordinator')); side.append(foot);
  return side;
}
function header() {
  const head = el('header', 'topbar');
  const context = el('div', 'workspace-context'); context.append(el('span', 'scope-dot'), el('span', '', 'Your workspace'), el('span', 'separator', '/'), el('span', 'workspace-section', state.view === 'fleet' ? 'Fleet' : state.view === 'data' ? 'Archive & costs' : state.view === 'task' ? 'Task detail' : 'Overview')); head.append(context);
  const actions = el('div', 'topbar-actions');
  if (state.snapshot) { const freshness = el('div', 'freshness'); freshness.title = `Panel snapshot generated ${exactTime(state.snapshot.generated_at)}`; freshness.append(icon('clock'), el('span', '', 'Snapshot'), timeNode(state.snapshot.generated_at)); actions.append(freshness); }
  const refresh = button(state.refreshing ? 'Refreshing' : 'Refresh', refreshSnapshot, 'button button-secondary button-small', 'refresh');
  refresh.disabled = !canCallTools() || state.refreshing; refresh.dataset.focusKey = 'refresh';
  refresh.title = canCallTools() ? 'Read the latest workspace snapshot' : 'Waiting for the ChatGPT connection';
  if (state.refreshing) refresh.classList.add('is-loading'); actions.append(refresh);
  const viewer = obj(state.snapshot?.viewer); const name = str(viewer.display_name || viewer.agent_id);
  if (name) { const avatar = el('span', 'avatar', name.slice(0, 1).toUpperCase()); avatar.title = name; avatar.setAttribute('aria-label', `Viewing as ${name}`); actions.append(avatar); }
  head.append(actions); return head;
}
function pageHeading(title: string, description: string, eyebrow = 'ASTER COORDINATOR') {
  const node = el('div', 'page-heading'); const text = el('div'); text.append(kicker(eyebrow), el('h1', '', title), el('p', 'page-subtitle', description)); node.append(text); return node;
}
function overview() {
  const page = el('section', 'page overview-page');
  page.append(pageHeading('Work overview', 'Assignments, outcomes, and the agents behind them.'));
  const stats = el('div', 'stats');
  const definitions: [string, number | null, string, string, string][] = [
    ['In progress', count('running'), 'Running assignments', 'activity', 'blue'],
    ['Queued', count('queued'), 'Waiting to be claimed', 'clock', 'muted'],
    ['Completed', count('completed'), 'Check result verification', 'check', 'teal'],
    ['Needs attention', sum('blocked', 'uncertain'), 'Blocked or uncertain', 'alert', 'amber'],
  ];
  for (const [title, value, sub, glyph, tone] of definitions) { const card = el('article', `stat-card ${tone}`); const top = el('div', 'stat-top'); top.append(el('span', '', title), icon(glyph)); card.append(top, el('strong', 'stat-value', value === null ? '—' : value), el('span', 'stat-note', sub)); stats.append(card); }
  page.append(stats);
  const layout = el('div', 'overview-layout');
  const card = el('section', 'card task-card');
  const title = el('div', 'section-title'); const titleLeft = el('div', 'title-inline'); titleLeft.append(el('h2', '', 'Assignments'), el('span', 'count-tag', tasks().length));
  title.append(titleLeft, el('span', 'subtle', count('total') !== null ? `${count('total')} in scope` : 'Current snapshot')); card.append(title);
  const controls = el('div', 'task-controls'); const search = el('label', 'search-box'); search.append(icon('search'));
  const input = el('input'); input.type = 'search'; input.placeholder = 'Find a task or agent'; input.setAttribute('aria-label', 'Find a task or agent'); input.value = state.search; input.dataset.focusKey = 'task-search';
  input.addEventListener('input', () => { state.search = input.value; render(); }); search.append(input); controls.append(search);
  const filters = el('div', 'filters'); filters.setAttribute('role', 'group'); filters.setAttribute('aria-label', 'Filter tasks by status');
  for (const key of ['all', 'running', 'queued', 'completed', 'blocked', 'uncertain', 'accepted']) {
    const filter = button(key === 'all' ? 'All tasks' : label(key), () => { state.filter = key; render(); }, `filter${state.filter === key ? ' selected' : ''}`);
    filter.setAttribute('aria-pressed', String(state.filter === key)); filter.dataset.focusKey = `filter-${key}`; filters.append(filter);
  }
  controls.append(filters); card.append(controls);
  const query = state.search.trim().toLowerCase();
  const visible = tasks().filter(t => (state.filter === 'all' || (state.filter === 'accepted' ? t.acceptance === 'accepted' || t.status === 'accepted' : t.status === state.filter)) && (!query || [taskTitle(t), taskId(t), taskReceiver(t), agentName(taskReceiver(t))].join(' ').toLowerCase().includes(query)));
  if (!visible.length) card.append(makeEmpty(tasks().length ? 'No matching assignments' : 'No assignments in this snapshot', tasks().length ? 'Try another status or search phrase.' : 'Refresh to check for new work. Tasks you can access will appear here.'));
  else {
    const table = el('div', 'task-list'); const labels = el('div', 'task-columns'); labels.append(el('span', '', 'TASK'), el('span', '', 'AGENT'), el('span', '', 'STATUS'), el('span', '', 'UPDATED'), el('span')); table.append(labels);
    for (const task of visible) table.append(taskRow(task)); card.append(table);
  }
  const foot = el('div', 'card-footer'); foot.append(el('span', '', `${visible.length} shown${state.search || state.filter !== 'all' ? ` of ${tasks().length} loaded` : ''}`), el('span', '', 'Source: Coordinator')); card.append(foot); layout.append(card);
  const rail = el('aside', 'overview-rail'); rail.append(sourceSummary(), fleetSummary()); layout.append(rail); page.append(layout);
  return page;
}
function taskRow(task: Dict) {
  const id = taskId(task); const row = el('button', 'task-row'); row.type = 'button'; row.dataset.focusKey = `task-${id}`;
  row.setAttribute('aria-label', `Open task: ${taskTitle(task)}`); row.addEventListener('click', () => openTask(task));
  const title = el('div', 'task-title'); title.append(el('strong', '', taskTitle(task)), el('span', 'mono task-id', id));
  const agent = el('span', 'task-agent', agentName(taskReceiver(task)));
  const updated = timeNode(task.updated_at || task.created_at, 'task-updated');
  row.append(title, agent, badge(task.status), updated, icon('arrow', 'row-arrow')); return row;
}
function sourceSummary() {
  const card = el('section', 'card signal-card'); card.append(el('h2', '', 'Workspace signals'));
  for (const [key, title, glyph] of [['coordination', 'Coordinator', 'activity'], ['archive', 'Archive', 'archive'], ['costs', 'Costs', 'document']]) {
    const data = source(key); const row = el('div', 'signal-row'); const heading = el('div', 'signal-label'); heading.append(icon(glyph), el('strong', '', title));
    row.append(heading, badge(data.status || 'not_reported')); card.append(row);
  }
  card.append(el('p', 'rail-note', 'Source status is reported by the server. A snapshot is a point in time.'));
  card.append(button('View sources', () => go('data'), 'text-button', 'arrow')); return card;
}
function fleetSummary() {
  const card = el('section', 'card fleet-summary'); const all = agents(); const ready = all.filter(a => a.receive_ready === true).length;
  card.append(kicker('FLEET READINESS'), el('div', 'fleet-summary-value', `${ready} / ${all.length}`), el('p', '', 'agents ready to receive'));
  const dots = el('div', 'fleet-dots'); for (const agent of all.slice(0, 8)) { const dot = el('span', `agent-mini ${agent.receive_ready === true ? 'ready' : ''}`, str(agent.display_name || agent.agent_id, '?').slice(0, 1).toUpperCase()); dot.title = `${agentName(agent.agent_id)} · ${agent.receive_ready === true ? 'Receive ready' : 'Not receive ready'}`; dots.append(dot); } card.append(dots);
  card.append(el('p', 'rail-note', 'Receive readiness is not a live connection or heartbeat.'), button('Explore fleet', () => go('fleet'), 'text-button', 'arrow')); return card;
}

function renderValue(value: unknown, depth = 0): HTMLElement {
  if (!hasValue(value)) return el('p', 'missing-value', 'Not reported');
  if (typeof value === 'string') {
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) && timestamp(value)) {
      const node = el('time', 'value-text', exactTime(value)); node.dateTime = timestamp(value)!.toISOString(); return node;
    }
    return el('p', 'value-text', value);
  }
  if (typeof value !== 'object') return el('p', 'value-text', value);
  if (depth >= 4) return el('pre', 'value-json', JSON.stringify(value, null, 2));
  if (Array.isArray(value)) { const node = el('ul', 'value-list'); for (const item of value.slice(0, 100)) { const li = el('li'); li.append(renderValue(item, depth + 1)); node.append(li); } if (value.length > 100) node.append(el('li', 'subtle', `${value.length - 100} additional items are omitted from this view.`)); return node; }
  const node = el('dl', 'value-dictionary'); for (const [key, item] of Object.entries(value as Dict).slice(0, 100)) { const pair = el('div', 'value-pair'); pair.append(el('dt', '', label(key))); const dd = el('dd'); dd.append(renderValue(item, depth + 1)); pair.append(dd); node.append(pair); } return node;
}
function detailCard(title: string, glyph: string, value: unknown, emptyMessage: string) {
  const card = el('section', 'card detail-card'); const head = el('div', 'detail-card-title'); head.append(icon(glyph), el('h2', '', title)); card.append(head);
  if (hasValue(value)) card.append(renderValue(value)); else card.append(el('p', 'missing-value', emptyMessage)); return card;
}
function taskView() {
  const page = el('section', 'page task-page'); const back = button('All assignments', () => go('overview'), 'text-button back-button', 'back'); back.dataset.focusKey = 'back-overview'; page.append(back);
  const task = state.detail || tasks().find(t => taskId(t) === state.selectedId);
  if (!task) { page.append(makeEmpty(state.loadingTask ? 'Loading task…' : 'Task not available', state.detailError || 'Return to the overview and refresh the workspace.')); return page; }
  const hero = el('div', 'task-heading'); const left = el('div'); left.append(kicker('TASK DETAIL'), el('h1', '', taskTitle(task)));
  const meta = el('div', 'task-meta'); meta.append(badge(task.status), el('span', 'mono', taskId(task)), el('span', '', `Assigned to ${agentName(taskReceiver(task))}`)); left.append(meta); hero.append(left);
  const refresh = button(state.loadingTask ? 'Refreshing' : 'Refresh task', () => openTask(task), 'button button-secondary', 'refresh'); refresh.disabled = !canCallTools() || !!state.loadingTask; refresh.dataset.focusKey = 'refresh-task'; hero.append(refresh); page.append(hero);
  if (state.loadingTask) page.append(el('div', 'inline-notice', 'Loading the latest task record…'));
  if (state.detailError) { const note = el('div', 'banner error'); note.setAttribute('role', 'alert'); note.append(icon('alert'), el('span', '', state.detailError)); page.append(note); }
  const layout = el('div', 'detail-layout'); const main = el('div', 'detail-main');
  const assignment = detailCard('Assignment', 'document', task.instruction, 'No instruction was included.');
  if (hasValue(task.expected_result)) { assignment.append(el('h3', 'subsection-label', 'Expected result'), renderValue(task.expected_result)); }
  if (hasValue(task.authorized_scope)) { const scope = el('details', 'disclosure'); scope.append(el('summary', '', 'Authorized scope'), renderValue(task.authorized_scope)); assignment.append(scope); }
  main.append(assignment);
  if (task.status_reason) main.append(detailCard('Status note', 'info', task.status_reason, ''));
  main.append(detailCard('Result', 'check', task.result, 'No result has been submitted.'));
  main.append(detailCard('Evidence', 'document', task.evidence, 'No supporting evidence has been recorded.'));
  if (hasValue(task.checkpoint)) main.append(detailCard('Latest checkpoint', 'activity', task.checkpoint, ''));
  layout.append(main);
  const rail = el('aside', 'detail-rail');
  const verification = el('section', 'card detail-card'); verification.append(el('h2', '', 'Verification'));
  const verificationState = task.result_verification || task.verification_status || obj(task.verification).status;
  verification.append(badge(verificationState || 'not_reported'), el('p', 'rail-note', 'Task completion and verification are separate records.'));
  const acceptance = el('div', 'label-value'); acceptance.append(el('span', '', 'Acceptance'), badge(task.acceptance || 'not_reported')); verification.append(acceptance);
  if (hasValue(task.verification_evidence || task.verification)) verification.append(renderValue(task.verification_evidence || task.verification)); rail.append(verification);
  const receipt = el('section', 'card detail-card'); receipt.append(el('h2', '', 'Receipt & record'));
  if (hasValue(task.receipt || task.result_receipt)) receipt.append(renderValue(task.receipt || task.result_receipt));
  else receipt.append(el('p', 'rail-note', 'No separate receipt supplied. Coordinator record details are shown below.'));
  const record = el('dl', 'record-list');
  for (const [key, value] of [['Task ID', taskId(task)], ['Revision', task.revision], ['From', agentName(task.sender)], ['Created', exactTime(task.created_at)], ['Updated', exactTime(task.updated_at)]]) { record.append(el('dt', '', key), el('dd', key === 'Task ID' ? 'mono' : '', value ?? 'Not reported')); } receipt.append(record); rail.append(receipt);
  const handoff = el('section', 'card handoff-card'); handoff.append(icon('chat'), el('h2', '', 'Put this task in context'), el('p', 'rail-note', 'Ask ChatGPT to review the result, evidence, and next step.'));
  const ask = button(state.sending ? 'Sending…' : 'Ask in chat about this task', () => askInChat(task), 'button button-primary', 'chat'); ask.disabled = !canSendMessage() || state.sending; ask.dataset.focusKey = 'ask-task'; handoff.append(ask);
  if (!canSendMessage()) handoff.append(el('small', 'subtle', 'Chat handoff is unavailable in this host.')); rail.append(handoff); layout.append(rail); page.append(layout);
  const provenance = el('div', 'page-provenance'); provenance.append(el('span', '', 'Source: Coordinator'), el('span', '', `Task updated ${exactTime(task.updated_at)}`)); if (state.detailEnvelope?.generated_at) provenance.append(el('span', '', `Read ${exactTime(state.detailEnvelope.generated_at)}`)); page.append(provenance); return page;
}

function fleetView() {
  const page = el('section', 'page fleet-page'); page.append(pageHeading('The fleet', 'Registered agents and their current receive readiness.'));
  const note = el('div', 'context-note'); note.append(icon('info'), el('span', '', 'Readiness describes whether an agent can receive work. It does not prove that the agent is online or actively working.')); page.append(note);
  const grid = el('div', 'fleet-grid');
  for (const agent of agents()) {
    const card = el('article', 'card agent-card'); const title = el('div', 'agent-card-head');
    const avatar = el('span', `agent-avatar ${agent.receive_ready === true ? 'ready' : ''}`, str(agent.display_name || agent.agent_id, '?').slice(0, 1).toUpperCase());
    const names = el('div'); names.append(el('h2', '', agent.display_name || agent.agent_id || 'Unnamed agent'), el('span', 'mono subtle', agent.agent_id || 'No ID')); title.append(avatar, names); card.append(title);
    const readiness = agent.receive_ready === true ? badge('ready', 'Receive ready') : agent.receive_ready === false ? badge('blocked', 'Not receive ready') : badge('not_reported', 'Readiness not reported'); card.append(readiness);
    const metrics = el('dl', 'agent-metrics');
    const values = [['Provider', agent.provider], ['Receive mode', agent.receive_mode], ['Enrollment', agent.active === true ? 'Active' : agent.active === false ? 'Disabled' : null], ['Binding', agent.native_binding_status], ['Heartbeat', agent.last_seen_at || agent.last_heartbeat_at ? exactTime(agent.last_seen_at || agent.last_heartbeat_at) : null]];
    for (const [key, value] of values) { metrics.append(el('dt', '', key), el('dd', '', value ? (key === 'Heartbeat' ? value : label(value)) : 'Not reported')); } card.append(metrics);
    if (Array.isArray(agent.capabilities) && agent.capabilities.length) { const tags = el('div', 'capability-tags'); for (const cap of agent.capabilities) tags.append(el('span', '', label(cap))); card.append(tags); }
    grid.append(card);
  }
  if (!agents().length) grid.append(makeEmpty('No agents in this snapshot', 'Refresh the workspace to read the current fleet.', 'fleet')); page.append(grid); page.append(el('div', 'page-provenance', `Source: Coordinator · Snapshot ${exactTime(state.snapshot?.generated_at)}`)); return page;
}

function dataView() {
  const page = el('section', 'page data-page'); page.append(pageHeading('Archive & costs', 'See what each source can report, and when it was checked.'));
  for (const notice of Array.isArray(state.snapshot?.source_notices) ? state.snapshot.source_notices : []) {
    if (typeof notice === 'string') { const note = el('div', 'context-note'); note.setAttribute('role', 'status'); note.append(icon('info'), el('span', '', notice)); page.append(note); }
  }
  const grid = el('div', 'source-grid');
  for (const [key, title, glyph, explanation] of [
    ['coordination', 'Coordinator', 'activity', 'Assignments, fleet, results, and verification.'],
    ['archive', 'Archive', 'archive', 'Persisted knowledge and archive availability.'],
    ['costs', 'Costs', 'document', 'Reported usage and spend, when available.'],
  ]) {
    const data = source(key); const card = el('section', 'card source-card'); const head = el('div', 'source-card-head'); head.append(el('div', 'source-icon'), badge(data.status || 'not_reported')); head.firstElementChild!.append(icon(glyph)); card.append(head, el('h2', '', title), el('p', 'page-subtitle', explanation));
    if (hasValue(data.message || data.reason || data.detail || data.note)) card.append(el('p', 'source-message', str(data.message || data.reason || data.detail || data.note)));
    const fields = el('dl', 'record-list'); fields.append(el('dt', '', 'Source'), el('dd', '', data.label || data.name || data.source || title), el('dt', '', 'Last checked'), el('dd', '', exactTime(sourceTime(data))));
    if (data.freshness) fields.append(el('dt', '', 'Freshness'), el('dd', '', typeof data.freshness === 'string' ? data.freshness : JSON.stringify(data.freshness))); card.append(fields);
    const excluded = new Set(['status', 'message', 'reason', 'detail', 'note', 'label', 'name', 'source', 'checked_at', 'generated_at', 'updated_at', 'fetched_at', 'as_of', 'freshness', 'url', 'context_url']);
    const extra = Object.fromEntries(Object.entries(data).filter(([field, value]) => !excluded.has(field) && hasValue(value)));
    if (Object.keys(extra).length) { card.append(el('h3', 'subsection-label', 'Reported data'), renderValue(extra)); }
    if (key === 'costs' && !Object.keys(extra).length) card.append(el('div', 'not-reported'), el('p', 'rail-note', 'No spend figure was supplied. Unreported cost does not mean zero cost.'));
    if (key === 'archive' && !Object.keys(extra).length) card.append(el('p', 'rail-note', 'This panel shows archive status. It does not infer archive contents or memory coverage.'));
    grid.append(card);
  }
  page.append(grid); const provenance = el('section', 'card provenance-card'); provenance.append(icon('clock'), el('div')); provenance.lastElementChild!.append(el('h2', '', 'A snapshot with a source'), el('p', '', `Panel snapshot generated ${exactTime(state.snapshot?.generated_at)}. Refresh reads a new panel snapshot. Each source keeps its own last-checked time.`)); page.append(provenance); return page;
}

function shellState() {
  const page = el('section', 'page'); page.append(pageHeading('Your workspace, in view', 'Tasks, results, agents, and sources in one place.'));
  const empty = makeEmpty(state.connected ? 'Waiting for the workspace snapshot' : 'Connecting to ChatGPT', state.connected ? 'The opening tool will supply your authorized workspace. You can also refresh when the connection is ready.' : 'Open Aster from its connected tool or workspace entry. No account data is loaded outside that connection.', 'activity'); empty.classList.add('initial-state'); page.append(empty); return page;
}
function render() {
  const active = document.activeElement as HTMLInputElement | null;
  const focusKey = active?.dataset?.focusKey;
  const selection = active && 'selectionStart' in active ? [active.selectionStart, active.selectionEnd] : null;
  const shell = el('div', 'app-shell'); shell.append(sidebar()); const workspace = el('main', 'workspace'); workspace.append(header());
  if (state.error || state.notice) { const banner = el('div', `banner ${state.error ? 'error' : 'notice'}`); banner.setAttribute('role', state.error ? 'alert' : 'status'); banner.append(icon(state.error ? 'alert' : 'check'), el('span', '', state.error || state.notice)); workspace.append(banner); }
  workspace.append(state.snapshot ? state.view === 'task' ? taskView() : state.view === 'fleet' ? fleetView() : state.view === 'data' ? dataView() : overview() : shellState());
  const footer = el('footer', 'workspace-footer'); footer.append(el('span', '', 'Aster · Personal coordination'), el('span', '', 'Read-only workspace')); workspace.append(footer); shell.append(workspace); root.replaceChildren(shell);
  if (focusKey) { const target = Array.from(root.querySelectorAll<HTMLElement>('[data-focus-key]')).find(n => n.dataset.focusKey === focusKey); if (target) { target.focus({ preventScroll: true }); if (selection && target instanceof HTMLInputElement) try { target.setSelectionRange(selection[0], selection[1]); } catch {} } }
}

function toolPayload(result: unknown): Dict {
  const wrapped = obj(result); if (wrapped.isError) throw new Error('The Aster tool could not complete this read. Your last snapshot has been kept.');
  const data = obj(wrapped.structuredContent); if (Object.keys(data).length) return data;
  for (const block of Array.isArray(wrapped.content) ? wrapped.content : []) {
    if (block.type === 'text' && typeof block.text === 'string') { try { const parsed = JSON.parse(block.text); if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed; } catch {} }
  }
  throw new Error('The tool returned no readable panel data. Refresh to try again.');
}
function receiveResult(result: unknown) {
  try {
    const payload = toolPayload(result);
    if (payload.schema_version === 'aster.panel.v1' && Array.isArray(payload.tasks)) { state.snapshot = payload; state.error = ''; }
    else if (payload.task && typeof payload.task === 'object') { const task = obj(payload.task); if (!state.selectedId || taskId(task) === state.selectedId) { state.detail = task; state.detailEnvelope = payload; state.detailError = ''; } }
    else throw new Error('This panel received an unsupported data format. Refresh or reopen Aster.');
  } catch (error) { state.error = error instanceof Error ? error.message : 'The panel could not read this response.'; }
  render();
}
function canCallTools() { return state.connected && !!app.getHostCapabilities()?.serverTools; }
function canSendMessage() { return state.connected && !!app.getHostCapabilities()?.message?.text; }
async function refreshSnapshot() {
  if (!canCallTools() || state.refreshing) return;
  state.refreshing = true; state.error = ''; state.notice = ''; render();
  try { receiveResult(await app.callServerTool({ name: 'aster_panel_refresh', arguments: { limit: 40 } })); }
  catch { state.error = 'Could not refresh Aster. The last snapshot is still shown; try again when the connection is available.'; }
  finally { state.refreshing = false; render(); }
}
async function openTask(task: Dict) {
  const id = taskId(task); if (!id) return;
  const request = ++state.requestSequence;
  state.selectedId = id; state.detail = task; state.detailEnvelope = null; state.view = 'task'; state.detailError = ''; state.notice = ''; state.error = '';
  if (!canCallTools()) { state.detailError = 'Showing the snapshot record. A live task read is unavailable in this host.'; render(); return; }
  state.loadingTask = id; render();
  try {
    const payload = toolPayload(await app.callServerTool({ name: 'aster_panel_task', arguments: { task_id: id } }));
    if (request !== state.requestSequence) return;
    if (!payload.task || taskId(obj(payload.task)) !== id) throw new Error('Task detail did not match the requested assignment.');
    state.detail = payload.task; state.detailEnvelope = payload;
  } catch { if (request === state.requestSequence) state.detailError = 'Could not load the latest task detail. The snapshot record is shown; refresh this task to retry.'; }
  finally { if (request === state.requestSequence) { state.loadingTask = ''; render(); } }
}
async function askInChat(task: Dict) {
  if (!canSendMessage() || state.sending) return;
  state.sending = true; state.notice = ''; state.error = ''; render();
  try {
    const result = await app.sendMessage({ role: 'user', content: [{ type: 'text', text: `Please review Aster task ${taskId(task)}. Read its latest record, summarize its status, result, evidence and verification, and tell me what needs attention. This is a request for review; do not change the task.` }] });
    if (obj(result).isError) throw new Error('Message was not accepted');
    state.notice = 'Task review requested in chat.';
  } catch { state.error = 'The chat request could not be sent. You can ask about this task in the composer using its task ID.'; }
  finally { state.sending = false; render(); }
}
function applyContext(context: any) {
  if (context?.styles?.variables) applyHostStyleVariables(context.styles.variables);
  const insets = obj(context?.safeAreaInsets);
  for (const edge of ['top', 'right', 'bottom', 'left']) if (typeof insets[edge] === 'number') document.documentElement.style.setProperty(`--safe-${edge}`, `${Math.max(0, insets[edge])}px`);
}

// The opening result must be handled before the bridge starts. No duplicate initial read.
app.ontoolresult = receiveResult;
app.onhostcontextchanged = applyContext;
app.onerror = () => { state.error = 'The ChatGPT connection needs attention. Your last workspace snapshot is retained.'; render(); };
// Age the displayed timestamps locally; this never polls a server or starts work.
const freshnessTimer = window.setInterval(() => {
  if (document.hidden) return;
  root.querySelectorAll<HTMLTimeElement>('time[data-relative-time]').forEach(node => { node.textContent = relativeTime(node.dateTime); });
}, 30_000);
app.onteardown = async () => { window.clearInterval(freshnessTimer); return {}; };
window.addEventListener('pagehide', () => window.clearInterval(freshnessTimer), { once: true });
render();
try { await app.connect(); state.connected = true; applyContext(app.getHostContext()); render(); }
catch { state.error = 'Aster could not connect to its host. Reopen the panel from ChatGPT to try again.'; render(); }
