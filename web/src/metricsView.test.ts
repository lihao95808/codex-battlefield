/**
 * 端点耗时面板显示逻辑的判据（P5-1 / P5-5）。
 *
 * 这一层要防的错只有一类：**把"还没量到"渲染成"很快"**。
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | P1 | `null` 耗时显示 `—`，**不显示 0ms** | 0ms 看起来像"快得看不见"，真相是"还没量到" |
 * | P2 | `null` 错误率显示 `—`，**不显示 0%** | 0% 读成"很稳"，真相是"没有样本" |
 * | P3 | 退化的排最前面（按倍数降序） | 埋在长列表里的退化等于没报 |
 * | P4 | 徽标优先报退化数 | 标题上看不出有异常 |
 */
import { describe, expect, it } from 'vitest';

import { badgeText, errorRateText, msText, orderedRoutes } from './metricsView';
import type { MetricsResponse, RouteMetrics } from './types';

function route(key: string, over: Partial<RouteMetrics> = {}): RouteMetrics {
  return {
    key,
    count: 10,
    errors: 0,
    error_rate: 0,
    last_status: 200,
    samples: 10,
    total_ms: 100,
    mean_ms: 10,
    max_ms: 20,
    p50_ms: 8,
    p95_ms: 12,
    p99_ms: 18,
    ...over,
  };
}

function metrics(over: Partial<MetricsResponse> = {}): MetricsResponse {
  return {
    routes: [route('GET /api/fast')],
    route_count: 1,
    sample_limit: 1000,
    quantiles: [0.5, 0.95, 0.99],
    baseline: {},
    regressions: [],
    uptime_seconds: 1,
    note: '',
    ...over,
  };
}

// ---------- P1 耗时 ----------

describe('P1 未量到的耗时给破折号，不给 0ms', () => {
  it('null 与 undefined 都给 —', () => {
    expect(msText(null)).toBe('—');
    expect(msText(undefined)).toBe('—');
  });

  it('真的 0 才显示 0.0ms', () => {
    expect(msText(0)).toBe('0.0ms');
  });

  it('保留一位小数', () => {
    expect(msText(12.34)).toBe('12.3ms');
    expect(msText(0.05)).toBe('0.1ms');
  });

  it('0 与 null 的结果必须不同（这是这一层存在的理由）', () => {
    expect(msText(0)).not.toBe(msText(null));
  });
});

// ---------- P2 错误率 ----------

describe('P2 没有样本的错误率给破折号', () => {
  it('null 给 —', () => {
    expect(errorRateText(null)).toBe('—');
    expect(errorRateText(undefined)).toBe('—');
  });

  it('真的 0 才显示 0.0%', () => {
    expect(errorRateText(0)).toBe('0.0%');
  });

  it('百分比保留一位小数', () => {
    expect(errorRateText(0.5)).toBe('50.0%');
    expect(errorRateText(0.0123)).toBe('1.2%');
  });
});

// ---------- P3 排序 ----------

describe('P3 退化的排在最前面', () => {
  it('退化行提到最前，并按倍数降序', () => {
    const data = metrics({
      routes: [
        route('GET /api/slow', { p95_ms: 500 }),
        route('GET /api/mild', { p95_ms: 20 }),
        route('GET /api/bad', { p95_ms: 100 }),
      ],
      regressions: [
        { key: 'GET /api/mild', baseline_p95_ms: 10, current_p95_ms: 20, ratio: 2, samples: 10 },
        { key: 'GET /api/bad', baseline_p95_ms: 10, current_p95_ms: 100, ratio: 10, samples: 10 },
      ],
    });
    expect(orderedRoutes(data).map((item) => item.key)).toEqual([
      'GET /api/bad',
      'GET /api/mild',
      'GET /api/slow',
    ]);
  });

  it('没有退化时保持后端给的顺序（已按 p95 排好）', () => {
    const data = metrics({ routes: [route('a'), route('b')] });
    expect(orderedRoutes(data).map((item) => item.key)).toEqual(['a', 'b']);
  });

  it('不改动入参数组', () => {
    const data = metrics({
      routes: [route('x'), route('y')],
      regressions: [{ key: 'y', baseline_p95_ms: 1, current_p95_ms: 5, ratio: 5, samples: 3 }],
    });
    orderedRoutes(data);
    expect(data.routes.map((item) => item.key)).toEqual(['x', 'y']);
  });
});

// ---------- P4 徽标 ----------

describe('P4 徽标优先报退化', () => {
  it('有退化时报退化数', () => {
    const data = metrics({
      route_count: 9,
      regressions: [{ key: 'a', baseline_p95_ms: 1, current_p95_ms: 5, ratio: 5, samples: 3 }],
    });
    expect(badgeText(data)).toBe('1 个退化');
  });

  it('没有退化时报端点数', () => {
    expect(badgeText(metrics({ route_count: 7 }))).toBe('7 个端点');
  });
});
