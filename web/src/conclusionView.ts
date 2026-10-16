/**
 * 结论文档面板的**纯显示逻辑**（P4-5）。
 *
 * 这一层要防的错只有一类：**让"还没写"看起来像"已经写完了"**。
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | R1 | `authored` 为空时**不许提交**，并说清要写什么 | 归档一堆只有事实的"结论" |
 * | R2 | 草稿要显眼标出"没落库" | 用户以为已经归档了，关掉页面就没了 |
 * | R3 | `incomplete` 非空时必须显示（不能折叠掉） | "没能确定的地方"被读成"确定没有" |
 * | R4 | 结论状态用可读标签（草稿/定稿），且区分 | "草稿"被当成定论 |
 */
import type { ConclusionDraft, ConclusionPayload } from './types';

export interface SubmitProblem {
  field: 'authored' | null;
  message: string;
}

/**
 * 提交前的本地校验。**后端会独立再校验一次**（它是权威），这里只是让用户少跑一趟。
 *
 * 为什么必须要求 `authored`：只有自动事实的文档不值得归档 —— 它只是另一份事件表，
 * 而且会因为"看起来像结论"而更少被人重新检查。
 */
export function validateConclusion(authored: string): SubmitProblem {
  if (!authored.trim()) {
    return {
      field: 'authored',
      message:
        '请写下「当时为什么这么决定」——只有事实的文档不值得归档，它只是另一份事件表。',
    };
  }
  return { field: null, message: '' };
}

/** 草稿状态一行：**必须写明没落库**。 */
export function draftStatusText(draft: ConclusionDraft): string {
  const facts = draft.facts ?? {};
  const nodes = facts.node_count ?? 0;
  const decisions = (facts.timeline ?? []).length;
  const extra = (facts.timeline_total ?? decisions) - decisions;
  return (
    `草稿（**没有落库**）：${nodes} 个节点、${decisions} 条决策点` +
    (extra > 0 ? `（另有 ${extra} 条未列出）` : '')
  );
}

/** 状态标签。`final` 只表示"人已经定稿"，不表示结论正确。 */
export const CONCLUSION_STATUS: Record<string, string> = {
  draft: '草稿（未定稿）',
  final: '已定稿',
};

export function conclusionStatusLabel(status: string): string {
  return CONCLUSION_STATUS[status] ?? `未知状态（${status}）`;
}

/**
 * "没能确定的地方"要显示成什么。
 *
 * **空数组也要给一句话**：那本身就是结论（"这份草稿的事实是齐全的"），
 * 而留空会让人以为渲染坏了。
 */
export function incompleteText(draft: ConclusionDraft | ConclusionPayload): string[] {
  const items = draft.incomplete ?? [];
  if (items.length === 0) {
    return ['（这份草稿里没有未确定项 —— 事实部分是齐全的。）'];
  }
  return items;
}

/** 导出文件名。带 id 便于区分多次归档。 */
export function exportFileName(conclusion: ConclusionPayload): string {
  return `conclusion-${conclusion.id}.md`;
}
