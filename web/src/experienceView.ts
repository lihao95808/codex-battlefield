/** OpenViking records are audit evidence, not a locally validated experience database. */
export type JsonObject = Record<string, unknown>;
export interface LearningRecord {
  id: string | number;
  task_id: string;
  kind: string;
  phase: string | null;
  attempt_key: string | null;
  status: string | null;
  payload: JsonObject;
  content_sha256: string | null;
  provider_task_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}
export interface LearningRecordsResponse {
  records: LearningRecord[];
  provider: { enabled: boolean; backend: string; configured: boolean };
}

export function asObject(value: unknown): JsonObject | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject : null;
}
export function knownText(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return '未知（未提供）';
}
export function jsonText(value: unknown): string {
  return value === undefined || value === null ? '未知（未提供）' : JSON.stringify(value, null, 2);
}
export function recordStatusLabel(status: unknown): string {
  const labels: Record<string, string> = {
    pending_extraction: '归档已接受 · 待提取（不代表已学习或验证）',
    accepted: '已接受（不代表已学习或验证）',
    recalled: '已召回（建议，不代表验证）',
    ok: '召回完成（建议，不代表验证）',
    pending: '待发送（尚未归档）',
    sending: '发送中（归档结果未知）',
    send_unknown: '发送结果未知（不能确认归档或学习）',
    completed: '操作完成（不代表经验验证）',
    skipped: '已跳过', disabled: '已禁用', failed: '失败', error: '错误',
    candidate: '候选建议（未验证）',
  };
  return typeof status === 'string' && status.trim()
    ? labels[status] ?? `未知状态（${status}）` : '状态未知（未提供）';
}
export function learnedLabel(value: unknown): string {
  if (value === false) return '未学习（提供方明确标记 false）';
  if (value === true) return '提供方标记已学习（不代表经验经过验证）';
  return '学习结果未知（未提供明确结果）';
}
export function publicationStatus(payload: JsonObject): unknown {
  return asObject(payload.publication)?.status;
}
export function recallEntries(payload: JsonObject): JsonObject[] {
  return Array.isArray(payload.entries)
    ? payload.entries.map(asObject).filter((entry): entry is JsonObject => entry !== null) : [];
}
export function recallIdentity(entry: JsonObject): { uri: string; hash: string } {
  return {
    uri: knownText(entry.uri),
    hash: knownText(entry.content_sha256 ?? entry.sha256 ?? entry.hash),
  };
}
export function recordsUrl(taskId: string | null): string {
  return '/api/learning/records' + (taskId ? `?task_id=${encodeURIComponent(taskId)}` : '');
}
export function filterRecords(records: LearningRecord[], query: string, kind: string, status: string) {
  const needle = query.trim().toLocaleLowerCase();
  return records.filter((record) => (!kind || record.kind === kind)
    && (!status || record.status === status)
    && (!needle || JSON.stringify(record).toLocaleLowerCase().includes(needle)));
}
/** A malformed response must never masquerade as a successful empty list. */
export function parseRecordsResponse(value: unknown): LearningRecordsResponse {
  const body = asObject(value);
  const provider = asObject(body?.provider);
  if (!Array.isArray(body?.records) || !provider || typeof provider.enabled !== 'boolean'
    || typeof provider.configured !== 'boolean' || typeof provider.backend !== 'string') {
    throw new Error('读取记忆记录失败：响应格式无效');
  }
  for (const value of body.records) {
    const record = asObject(value);
    if (!record || (typeof record.id !== 'string' && typeof record.id !== 'number')
      || typeof record.task_id !== 'string' || typeof record.kind !== 'string'
      || !asObject(record.payload)) {
      throw new Error('读取记忆记录失败：记录格式无效');
    }
  }
  return value as LearningRecordsResponse;
}
