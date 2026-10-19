import { useEffect, useRef, useState } from 'react';
import { fetchRunArtifact } from './api';
import { readableRunLog, taskImageArtifacts } from './nodeInspection';
import type { RunArtifactResponse, TaskDetailResponse } from './types';

export function NodeActivity({ detail }: { detail: TaskDetailResponse }) {
  const runs = [...detail.runs].sort((a, b) => b.id - a.id);
  const [chosen, setChosen] = useState<number | null>(null);
  const run = runs.find((item) => item.id === chosen) ?? runs[0];
  const runId = run?.id;
  const [kind, setKind] = useState<RunArtifactResponse['kind']>('logs');
  const [snapshot, setSnapshot] = useState<{ key: string; value: RunArtifactResponse } | null>(null);
  const [error, setError] = useState('');
  const [follow, setFollow] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const requestKey = `${detail.task.id}:${run?.id}:${kind}`;
  const data = snapshot?.key === requestKey ? snapshot.value : null;
  const images = taskImageArtifacts(detail.task);

  useEffect(() => {
    if (runId === undefined) return;
    let alive = true;
    let fetching = false;
    const load = async () => {
      if (fetching || document.hidden) return;
      fetching = true;
      try {
        const value = await fetchRunArtifact(runId, kind);
        if (alive) { setSnapshot({ key: requestKey, value }); setError(''); }
      } catch (e) { if (alive) setError(e instanceof Error ? e.message : String(e)); }
      finally { fetching = false; }
    };
    void load();
    const timer = setInterval(() => { void load(); }, 5000);
    return () => { alive = false; clearInterval(timer); };
  }, [runId, detail.task.id, kind, requestKey]);

  useEffect(() => {
    if (follow && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [data?.content, follow]);

  return <>
    <section className="detail-section node-activity" aria-label="节点聊天与运行记录">
      <h3>Agent 当前过程 · 公开摘要 / 执行记录</h3>
      <p className="muted">仅显示模型已输出的消息、公开摘要与工具日志，不包含未公开的内部思考。每5秒更新。</p>
      {runs.length > 0 ? <>
        <label>运行轮次<select aria-label="选择节点运行轮次" value={run?.id ?? ''} onChange={(e) => { setChosen(Number(e.target.value)); setSnapshot(null); setError(''); }}>
          {runs.map((item) => <option key={item.id} value={item.id}>#{item.id} · {item.kind} · {item.status}</option>)}
        </select></label>
        <div className="node-log-tools">
          {(['logs', 'last_message', 'prompt'] as const).map((tab) => <button key={tab} type="button" aria-pressed={kind === tab} onClick={() => { setKind(tab); setError(''); }}>{tab === 'logs' ? '聊天与日志' : tab === 'last_message' ? '最后回复' : '提示词'}</button>)}
          <label><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />跟随最新</label>
        </div>
        {error && <p role="alert" className="artifact-error">{error}</p>}
        {data?.truncated && <p className="muted">大文件仅显示最近尾部记录；此处不是完整历史。</p>}
        <div ref={scrollRef} className="node-log-scroll" tabIndex={0} aria-label="可滚动的节点聊天记录">
          {!data && !error && <p>正在读取记录…</p>}
          {data && (!data.exists ? <p>该轮没有记录文件。</p> : !data.content ? <p>日志文件目前为空。</p> : kind === 'logs'
            ? readableRunLog(data.content).map((item) => <article className="node-log-entry" key={item.id}><strong>{item.label}</strong><pre>{item.text}</pre></article>)
            : <pre>{data.content}</pre>)}
        </div>
      </> : <div className="node-log-scroll" tabIndex={0} aria-label="可滚动的节点聊天记录"><p className="muted">该节点还没有运行轮次，不会编造聊天或思考记录。</p></div>}
    </section>
    <section className="detail-section node-art-gallery" aria-label="美术产出预览">
      <h3>美术资源 / 截图</h3>
      {images.length ? <div className="node-art-grid">{images.map((image) => <ImagePreview key={image.path} image={image} />)}</div>
        : <p className="muted">暂未在此节点交付记录中发现图片资源。</p>}
    </section>
  </>;
}

export function ImagePreview({ image }: { image: { path: string; name: string; url: string } }) {
  const [failed, setFailed] = useState(false);
  return <a className="node-art-item" href={image.url} target="_blank" rel="noopener noreferrer" title={image.path}>
    {failed ? <span>预览不可用（文件缺失或不在授权目录）</span> : <img src={image.url} alt={image.name} loading="lazy" onError={() => setFailed(true)} />}
    <span>{image.name}</span>
  </a>;
}
