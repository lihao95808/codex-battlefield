import { describe, expect, it } from 'vitest';
import {
  asObject, filterRecords, jsonText, knownText, learnedLabel, parseRecordsResponse,
  publicationStatus, recallEntries, recallIdentity, recordsUrl, recordStatusLabel,
  type LearningRecord,
} from './experienceView';

const record = (overrides: Partial<LearningRecord> = {}): LearningRecord => ({
  id: 1, task_id: 'node-1', kind: 'review', phase: 'integration', attempt_key: 'a-1',
  status: 'pending_extraction', payload: { facts: { tests: 'passed' }, learned: false },
  content_sha256: 'abc', provider_task_id: null, created_at: null, updated_at: null, ...overrides,
});

describe('processing state must not imply experience verification', () => {
  it('accepted archive remains pending extraction, not learned', () => {
    expect(recordStatusLabel('pending_extraction')).toContain('归档已接受');
    expect(recordStatusLabel('pending_extraction')).toContain('不代表已学习或验证');
    expect(recordStatusLabel('completed')).toContain('不代表经验验证');
    expect(learnedLabel(false)).toContain('未学习');
  });
  it('recall and candidate statuses remain advisory', () => {
    expect(recordStatusLabel('ok')).toContain('建议');
    expect(recordStatusLabel('candidate')).toContain('未验证');
  });
  it('pending and sending are not accepted archives', () => {
    expect(recordStatusLabel('pending')).toContain('尚未归档');
    expect(recordStatusLabel('sending')).toContain('结果未知');
    expect(recordStatusLabel('send_unknown')).toContain('不能确认归档或学习');
  });
  it('unknown status is preserved and absent status remains unknown', () => {
    expect(recordStatusLabel('new-status')).toContain('new-status');
    for (const value of [null, undefined, '']) expect(recordStatusLabel(value)).toContain('未知');
  });
  it('missing outcomes never imply learned or successful', () => {
    for (const value of [null, undefined, 'true', 1, {}, 'completed']) {
      expect(learnedLabel(value)).toContain('未知');
    }
    expect(learnedLabel(true)).toContain('不代表经验经过验证');
    expect(publicationStatus({})).toBeUndefined();
    expect(publicationStatus({ publication: { status: 'completed' }, learned: false })).toBe('completed');
  });
});

describe('exact recall evidence and unknown-safe details', () => {
  it('shows full URI and hash without truncation or inferred identity', () => {
    const uri = 'viking://user/memories/a?version=3';
    const sha256 = '0123456789abcdef'.repeat(4);
    expect(recallIdentity({ uri, sha256 })).toEqual({ uri, hash: sha256 });
    expect(recallIdentity({ content_sha256: 'primary', sha256: 'secondary' }).hash).toBe('primary');
    expect(recallIdentity({}).uri).toContain('未知');
    expect(recallIdentity({}).hash).toContain('未知');
  });
  it('extracts object entries without fabricating missing recall entries', () => {
    expect(recallEntries({ entries: [null, 4, { uri: 'viking://a' }] })).toEqual([{ uri: 'viking://a' }]);
    expect(recallEntries({})).toEqual([]);
    expect(asObject([])).toBeNull();
  });
  it('keeps false, zero and structured facts distinct from unknown', () => {
    expect(knownText(false)).toBe('false');
    expect(knownText(0)).toBe('0');
    expect(knownText(null)).toContain('未知');
    expect(jsonText(undefined)).toContain('未知');
    expect(jsonText(null)).toContain('未知');
    expect(jsonText({ success: false })).toContain('"success": false');
  });
});

describe('read-only endpoint and local filtering', () => {
  it('queries all records without selection and encodes selected task id', () => {
    expect(recordsUrl(null)).toBe('/api/learning/records');
    expect(recordsUrl('a/b &c')).toBe('/api/learning/records?task_id=a%2Fb%20%26c');
  });
  it('combines text, kind and status while preserving input snapshots', () => {
    const records = [record(), record({ id: 2, kind: 'recall', status: 'ok', payload: { entries: [{ uri: 'viking://Hello' }] } })];
    expect(filterRecords(records, ' HELLO ', 'recall', 'ok')).toEqual([records[1]]);
    expect(filterRecords(records, 'passed', 'review', 'pending_extraction')).toEqual([records[0]]);
    expect(filterRecords(records, '', '', '')).toHaveLength(2);
    expect(filterRecords(records, 'no-match', '', '')).toEqual([]);
    expect(records).toHaveLength(2);
  });
  it('accepts explicit successful empty list, not malformed response', () => {
    const provider = { backend: 'openviking', enabled: false, configured: false };
    expect(parseRecordsResponse({ records: [], provider }).records).toEqual([]);
    expect(parseRecordsResponse({ records: [record()], provider }).records).toHaveLength(1);
    for (const value of [null, {}, { records: [] }, { records: null, provider }, { records: [null], provider }, { records: [{ id: 1 }], provider }]) {
      expect(() => parseRecordsResponse(value)).toThrow('格式无效');
    }
  });
});
