/**
 * 花费归因与成本预测的**纯显示逻辑**（P3-3 / P3-2）。
 *
 * 和 `crossProject.ts` 同样的理由：这里有几条"显示错了比不显示更糟"的判断，
 * 必须能被测试直接调用，而不是埋在 JSX 里靠人肉 review。
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | Z1 | 预测**必须显示区间**与样本量，不能只显示一个数 | 单一数字被读成"确定要花这么多"，用户据此决定去睡觉 |
 * | Z2 | 预测不可用时显示**原因**，不显示 0 | 0 会被读成"不用再花钱了" |
 * | Z3 | 归因表按金额排序时**同一币种内比**，不跨币种 | 跨币种排序等于假设了汇率 |
 * | Z4 | 未知模型/无用量**显眼报出** | 归因表看起来完整，其实缺了一大块 |
 */
import type { CostProjection, CostReport } from './types';

export interface NamedCostRow {
  key: string;
  label: string;
  runs: number;
  totalTokens: number;
  /** 每个币种一行 —— 不做跨币种合计。 */
  costs: { code: string; amount: number }[];
}

/** 把后端的一个桶转成表格行。 */
export function bucketRow(
  bucket: { key: string; runs: number; total_tokens: number; cost_by_currency?: Record<string, number> | null },
  label?: string,
): NamedCostRow {
  return {
    key: bucket.key,
    label: label ?? bucket.key,
    runs: bucket.runs,
    totalTokens: bucket.total_tokens,
    costs: Object.entries(bucket.cost_by_currency ?? {})
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([code, amount]) => ({ code, amount })),
  };
}

/** 金额文本。小到 0.01 以下时多留几位，否则看起来像 0。 */
export function amountText(code: string, amount: number): string {
  const digits = amount !== 0 && Math.abs(amount) < 0.01 ? 6 : 4;
  return `${code} ${amount.toFixed(digits)}`;
}

export interface ProjectionText {
  available: boolean;
  /** 不可用时的原因（必须显示）。 */
  reason: string;
  /** 区间文本，如 `USD 20.0000 – 40.0000`。 */
  range: string;
  /** "基于 N 个已计价节点" —— 样本量必须跟着数字一起出现。 */
  basis: string;
  /** 要不要显眼提醒样本太小。 */
  weakSample: boolean;
}

/** 样本量少于这个数时提醒"预测很粗"。不是统计阈值，是"别拿它当承诺"。 */
export const WEAK_SAMPLE_NODES = 5;

/**
 * 预测的文字化。
 *
 * **绝不只给一个数**：区间 + 样本量 + 依据必须同时出现。这条是本模块存在的
 * 主要理由 —— 一个孤零零的"预计还需 12.34 元"会被当成承诺。
 */
export function projectionText(projection: CostProjection): ProjectionText {
  if (!projection.available) {
    return {
      available: false,
      reason: projection.reason || '算不出来（原因未给出）',
      range: '',
      basis: '',
      weakSample: false,
    };
  }
  const code = projection.currency ?? '（未标币种）';
  const low = projection.low ?? 0;
  const high = projection.high ?? 0;
  const samples = projection.sample_nodes ?? 0;
  return {
    available: true,
    reason: '',
    range:
      low === high
        ? amountText(code, low)
        : `${amountText(code, low)} – ${amountText(code, high)}`,
    basis:
      `基于 ${samples} 个已计价节点` +
      (projection.retry_factor && projection.retry_factor !== 1
        ? `，重试系数 ${projection.retry_factor}`
        : ''),
    weakSample: samples < WEAK_SAMPLE_NODES,
  };
}

/**
 * 归因表里的"缺数据"提醒。
 *
 * 这两条不报，归因表看起来是完整的，而它其实漏掉了一部分花费 ——
 * 而"看起来完整"正是最容易被信的那种错。
 */
export function attributionWarnings(report: CostReport): string[] {
  const warnings: string[] = [];
  const attribution = report.attribution;
  if (attribution.runs_without_usage > 0) {
    warnings.push(
      `${attribution.runs_without_usage} 次运行没有可识别的用量数据` +
        `（共 ${attribution.runs} 次）—— 下面的金额**不含**它们。`,
    );
  }
  if (attribution.runs_unknown_model > 0) {
    warnings.push(
      `${attribution.runs_unknown_model} 次运行没有记录模型名 —— ` +
        `按模型拆的那张表**不含**它们（它们的花费仍在合计里）。`,
    );
  }
  return warnings;
}

/** 一个桶里最大的那个币种金额（用于排序）。没有金额时是 0。 */
export function topAmount(row: NamedCostRow): number {
  return row.costs.reduce((max, item) => Math.max(max, item.amount), 0);
}

/**
 * 按花费从大到小排序。
 *
 * 注意这是**按金额数值**排，而金额可能是不同币种的。所以只在"同一棵树、同一计价
 * 通道"下用得住；跨币种比较大小在这里没有意义，界面必须同时把币种显示出来
 * （`bucketRow` 保留每行自己的币种，正是为此）。
 */
export function sortRowsByCost(rows: NamedCostRow[]): NamedCostRow[] {
  return [...rows].sort((a, b) => {
    const delta = topAmount(b) - topAmount(a);
    return delta !== 0 ? delta : b.totalTokens - a.totalTokens;
  });
}
