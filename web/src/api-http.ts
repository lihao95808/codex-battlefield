/**
 * HTTP 客户端的小工具（P5-2：从 `api.ts` 拆出来）。
 *
 * `readDetail` 在 `api.ts` 里是私有函数；拆成多个领域文件后三处都要用它，
 * 所以提到这里导出 —— 错误消息的解析只留一份实现。
 */

/** 把后端 `detail` 抽出来（FastAPI 的错误体）。抽不到就退回状态码。 */
export async function readDetail(response: Response, fallback: string): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    if (body.detail) return body.detail;
  } catch {
    // 不是 JSON 就走下面的 fallback。
  }
  return `${fallback}：${response.status}`;
}
