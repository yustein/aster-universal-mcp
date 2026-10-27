const now = Date.now();
const iso = ago => new Date(now - ago * 60_000).toISOString();
const base = { sender: 'aster', revision: 3, fence: 1, created_at: iso(120), updated_at: iso(8), result_verification: 'not_submitted', acceptance: 'pending' };
export const snapshot = {
  schema_version: 'aster.panel.v1', generated_at: iso(0),
  viewer: { agent_id: 'aster', display_name: 'UI fixture' },
  counts: { queued: 2, running: 2, completed: 2, blocked: 1, uncertain: 1, accepted: 1, total: 8 },
  tasks: [
    { ...base, task_id: 'task_launch_review', receiver: 'research', status: 'running', instruction: 'Review the launch plan and supporting research', expected_result: 'A concise findings report with primary sources.', updated_at: iso(2), checkpoint: { phase: 'Reviewing sources', sources_reviewed: 7 } },
    { ...base, task_id: 'task_archive_index', receiver: 'archive', status: 'completed', instruction: 'Check the archive index and document coverage', result_verification: 'sender_verified', acceptance: 'accepted', updated_at: iso(7) },
    { ...base, task_id: 'task_weekly_brief', receiver: 'writer', status: 'queued', instruction: 'Prepare the next weekly editorial brief', updated_at: iso(14) },
    { ...base, task_id: 'task_source_access', receiver: 'research', status: 'blocked', instruction: 'Verify the source records for the latest report', status_reason: 'The required source is unavailable.', updated_at: iso(23) },
    { ...base, task_id: 'task_handoff_receipt', receiver: 'archive', status: 'uncertain', instruction: 'Reconcile the final handoff receipt', status_reason: 'An outcome was reported without confirmation of delivery.', updated_at: iso(35) },
    { ...base, task_id: 'task_promo_outline', receiver: 'writer', status: 'running', instruction: 'Shape the promotional film outline', updated_at: iso(44) },
    { ...base, task_id: 'task_content_audit', receiver: 'research', status: 'completed', instruction: 'Audit content references and attribution', result_verification: 'worker_reported', updated_at: iso(55) },
    { ...base, task_id: 'task_next_review', receiver: 'writer', status: 'queued', instruction: 'Organize the next review session', updated_at: iso(64) },
  ],
  agents: [
    { agent_id: 'research', display_name: 'Research', provider: 'OpenAI', receive_mode: 'native', active: true, receive_ready: true, native_binding_status: 'operator_verified', capabilities: ['research', 'source_review'] },
    { agent_id: 'writer', display_name: 'Writer', provider: 'Anthropic', receive_mode: 'pull', active: true, receive_ready: true, native_binding_status: 'not_configured', capabilities: ['writing', 'editorial'] },
    { agent_id: 'archive', display_name: 'Archive', provider: 'Local', receive_mode: 'native', active: true, receive_ready: false, native_binding_status: 'pending_or_expired', capabilities: ['archive', 'retrieval'] },
  ],
  sources: {
    coordination: { status: 'available', label: 'Coordinator · test fixture', checked_at: iso(0), message: 'Authorized assignments and agents are available.', tasks_returned: 8 },
    archive: { status: 'not_connected', label: 'Archive · test fixture', checked_at: iso(0), message: 'The archive source is not connected to this view.' },
    costs: { status: 'not_reported', label: 'Costs · test fixture', checked_at: iso(0), message: 'No usage ledger was supplied.' },
  },
};
export const detail = {
  schema_version: 'aster.panel.task.v1', generated_at: iso(0),
  task: {
    ...snapshot.tasks[1],
    instruction: 'Check the archive index and document coverage. Confirm which records are available and record any gap that needs follow-up.',
    expected_result: 'An evidence-backed coverage report and a clear list of unresolved gaps.',
    authorized_scope: { mode: 'Read only', changes: 'No archive records may be changed.' },
    result: { summary: 'The index is available and the requested records were found.', coverage: '12 of 12 expected records are present.', next_step: 'Review the two records whose source dates need confirmation.' },
    evidence: { reference: 'archive://checks/coverage-review', checks: ['Read the index metadata.', 'Compared the requested IDs with the retrieved records.', 'Confirmed all twelve expected entries exist.'] },
    verification_evidence: { reference: 'review://coverage-review', checks: ['Source IDs were checked against the original request.'], note: 'Accepted after sender review.' },
    receipt: { receipt_id: 'receipt_coverage_review', outcome: 'Accepted', recorded_at: iso(7) },
  },
};
