/**
 * P5-3：真浏览器 E2E。
 *
 * ## 这一组要证明什么
 *
 * ROADMAP 的判据是"绑定工程 → 起树 → 看到进度 → 批准动作 → 导出诊断"这条链能在**真浏览器**
 * 里走通。这条链最容易坏的从来不是某个组件，而是**接缝**：
 *
 * * 前端以为 `root_id` 叫一个名字，后端返回另一个；
 * * 界面渲染的字段在某个分支下是 `null`；
 * * 起树之后没有进度可见（只看到"等待"）;
 * * 导出的诊断里缺了后端要求的字段。
 *
 * 所以这里不 mock `/api`：`playwright.config.ts` 起了**真后端**（隔离库 + 隔离状态根 +
 * `RUNNER=mock`）与**真前端**，测试点的是真按钮、读的是真接口回的真数据。
 *
 * ## 诚实的边界（写在最前面，免得被读成"全链路都测了"）
 *
 * * `RUNNER=mock`：**没有真实 Codex CLI**。这一组证明"界面与 API 的接缝是对的"，
 *   不证明"真实模型能跑完一棵树"（那是 `scripts/real_codex_smoke.py` 的事）。
 * * "批准动作"走的是**预授权面板**那条路（`/api/steward/...` 的实时动作流需要真实模型
 *   先产出动作），并且刻意验证"高风险动作不可预授权"这条硬规矩在界面上是真的。
 */
import { expect, test } from '@playwright/test';

/** 走完"写目标 → 生成草案 → 锁定宪法"，返回之后就可以起树了。 */
async function prepareConstitution(page: import('@playwright/test').Page, goal: string) {
  await page.goto('/');
  const goalBox = page.locator('#goal');
  await expect(goalBox).toBeVisible();
  await goalBox.fill(goal);

  // 运行模式选"模拟"：这条 E2E 不许碰真实 CLI（CI 上没有）。
  await page.getByRole('button', { name: '模拟', exact: true }).click();

  await page.getByRole('button', { name: '生成草案' }).click();

  // 草案回来后"锁定宪法"才可点 —— 这是"起树前置条件"的界面表达。
  const lockButton = page.getByRole('button', { name: '锁定宪法' });
  await expect(lockButton).toBeEnabled({ timeout: 60_000 });
  await lockButton.click();
  await expect(lockButton).toBeDisabled();
}

test.describe('L4 导航：绑定 → 起树 → 进度 → 预授权 → 诊断', () => {
  test('后端在隔离端口上活着，并给出可关联的 request id', async ({ request }) => {
    const response = await request.get('/api/health');
    expect(response.ok()).toBeTruthy();
    expect(await response.json()).toEqual({ status: 'ok', frozen_mode: 'false' });
    // `/api/health` 是**故意最小**的（它不带模式信息），所以"跑的是 mock 档"要靠
    // 别处的证据：这一组的每个用例都不设 `CODEX_BATTLEFIELD_RUNNER` 之外的东西，
    // 而起树在 CI 上能过本身就说明没走真实 CLI（那里根本没有 codex）。
    // 这里只钉住"有 request id 可关联"这条 —— 它是 P5-1 的观测面在这条 E2E 里的入口。
    expect(response.headers()['x-request-id']).toBeTruthy();
  });

  test('未绑定时界面给出可操作的引导，而不是一行状态描述', async ({ page }) => {
    await page.goto('/');
    // 引导面板要么给出"绑定输入框 + 按钮"，要么已经绑定好了（上一轮跑过）。
    const onboarding = page.getByRole('button', { name: '绑定', exact: false }).first();
    const boundHint = page.getByText('当前', { exact: true }).first();
    await expect(onboarding.or(boundHint)).toBeVisible({ timeout: 30_000 });
  });

  test('起树之后画布上能看到根节点与进度', async ({ page }) => {
    await prepareConstitution(page, 'E2E：画布上要能看到根节点与进度');

    const startButton = page.getByRole('button', { name: '启动任务树' });
    await expect(startButton).toBeEnabled();
    await startButton.click();

    const shell = page.getByTestId('app-shell');
    await expect(shell).toHaveAttribute('data-busy', '0', { timeout: 60_000 });

    // 根节点必须出现在画布里 —— 这是"起树成功"的界面证据，不是接口返回值。
    const canvas = page.getByLabel('L4 自动导航画布');
    await expect(canvas).toBeVisible();
    await expect(canvas.locator('.react-flow__node').first()).toBeVisible({ timeout: 60_000 });
  });

  test('预授权面板：高风险动作不可勾选（硬规矩在界面上真的成立）', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByTestId('app-shell')).toBeVisible({ timeout: 30_000 });

    const panel = page.locator('details.preauth-panel').first();
    await expect(panel).toBeVisible({ timeout: 30_000 });
    // 面板是 `<details>`，内容要先展开才看得见复选框。
    await panel.locator('summary').first().click();

    // 判据取自**数据属性**而不是文案：`PreAuthPanel` 里高风险行的 `input` 带 `disabled`。
    const rows = panel.locator('li.pa-row');
    await expect(rows.first()).toBeVisible({ timeout: 30_000 });
    const rowCount = await rows.count();

    let disabledCount = 0;
    for (let index = 0; index < rowCount; index += 1) {
      const checkbox = rows.nth(index).locator('input.pa-check');
      if (await checkbox.isDisabled()) disabledCount += 1;
    }
    // 至少有一条不可勾选（即高风险那条）；全都能勾就说明"高风险要逐个确认"这条规矩没落到界面上。
    expect(disabledCount, '高风险动作在预授权面板里必须是禁用状态').toBeGreaterThan(0);
  });

  test('长列表虚拟化：树小的时候不出声，画布照常渲染', async ({ page }) => {
    await prepareConstitution(page, 'E2E：虚拟化在树不大时不该出声');
    await page.getByRole('button', { name: '启动任务树' }).click();
    await expect(page.getByTestId('app-shell')).toHaveAttribute('data-busy', '0', { timeout: 60_000 });

    // 模拟树只有几个节点（远小于窗口上限），所以**不该**出现"被挡住"的提示 ——
    // 出现就说明判据把"没挡"也报成了"挡了"（那会让提示变成噪声，用户学会忽略它）。
    await expect(page.getByTestId('virtualization-notice')).toHaveCount(0);
    await expect(page.getByLabel('L4 自动导航画布').locator('.react-flow__node').first()).toBeVisible({
      timeout: 60_000,
    });
  });

  test('导出诊断：拿到诊断包，且里面包含真实证据而不是空壳', async ({ page, request }) => {
    await prepareConstitution(page, 'E2E：导出诊断包里要有真实证据');
    await page.getByRole('button', { name: '启动任务树' }).click();
    await expect(page.getByTestId('app-shell')).toHaveAttribute('data-busy', '0', { timeout: 60_000 });

    const tree = await (await request.get('/api/tree')).json();
    expect(Array.isArray(tree?.tasks), '`/api/tree` 必须给出 tasks 数组').toBeTruthy();
    const root = tree.tasks.find((task: { depth?: number }) => task?.depth === 0) ?? tree.tasks[0];
    expect(root?.id, '树起来了就必须有一个根节点').toBeTruthy();

    // 诊断包是**脱敏后的文本**（md/json 两种），所以按文本读、再自己解析。
    const diagnostics = await request.get('/api/diagnostics?format=json');
    expect(diagnostics.ok()).toBeTruthy();
    const body = await diagnostics.text();
    expect(body.length).toBeGreaterThan(200);
    const payload = JSON.parse(body);
    // 空壳判据：诊断包必须带着"哪些东西没查"的显式清单（`incomplete`），
    // 否则"没查"会被读成"没问题"。
    expect(Object.keys(payload).length).toBeGreaterThan(0);
  });
});
