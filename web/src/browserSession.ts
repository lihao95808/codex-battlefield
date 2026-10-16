export function rootIdFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get('root_id');
}

export function writeRootIdToUrl(rootId: string | null) {
  const url = new URL(window.location.href);
  if (rootId) {
    url.searchParams.set('root_id', rootId);
  } else {
    url.searchParams.delete('root_id');
  }
  window.history.replaceState({}, '', url);
}

export function previewUrlForRoot(rootId: string): string {
  const url = new URL(window.location.href);
  url.searchParams.set('root_id', rootId);
  return url.toString();
}

export function openPendingPreviewWindow(): Window | null {
  try {
    const previewWindow = window.open('', '_blank');
    if (!previewWindow) return null;
    previewWindow.document.title = 'L4 Navigator';
    previewWindow.document.body.innerHTML = `
      <main style="min-height:100vh;display:grid;place-items:center;margin:0;background:#f4f8fb;color:#102033;font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
        <section style="text-align:center;">
          <div style="width:220px;height:6px;overflow:hidden;border-radius:999px;background:#d9e7ef;margin:0 auto 18px;">
            <div style="width:45%;height:100%;border-radius:999px;background:#1f7a93;animation:move 1.1s infinite ease-in-out;"></div>
          </div>
          <h1 style="font-size:20px;margin:0 0 8px;">L4 导航正在规划任务树</h1>
          <p style="font-size:14px;margin:0;color:#526577;">创建完成后会自动切换到可视化导航视图。</p>
        </section>
        <style>@keyframes move{0%{transform:translateX(-120%)}100%{transform:translateX(240%)}}</style>
      </main>
    `;
    return previewWindow;
  } catch {
    return null;
  }
}

export function navigatePreviewWindow(previewWindow: Window | null, url: string) {
  if (previewWindow) {
    previewWindow.location.href = url;
    return;
  }
  window.open(url, '_blank', 'noopener,noreferrer');
}
