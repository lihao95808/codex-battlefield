import { useEffect, useState } from 'react';
import { ChevronRight, File, Folder, RefreshCw } from 'lucide-react';

type Entry = { name: string; path: string; kind: 'directory' | 'file' };
type Listing = { root: string | null; entries: Entry[]; truncated: boolean; status: string };

function Directory({ path, onSelect }: { path: string; onSelect: (path: string) => void }) {
  const [data, setData] = useState<Listing | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    fetch(`/api/project/files?path=${encodeURIComponent(path)}`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error('目录读取失败');
        const result = await response.json() as Listing;
        if (!Array.isArray(result.entries)) throw new Error('目录数据不可用');
        setData(result); setError('');
      }).catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '目录读取失败');
      });
    return () => controller.abort();
  }, [path]);
  if (error) return <p className="onb-error" role="alert">{error}</p>;
  if (!data) return <p className="explorer-hint">读取目录…</p>;
  if (data.status === 'unbound') return <p className="explorer-hint">先绑定工程，文件会显示在这里。</p>;
  return <ul className="explorer-list">
    {data.entries.map((entry) => entry.kind === 'directory'
      ? <DirectoryItem key={entry.path} entry={entry} onSelect={onSelect} />
      : <li key={entry.path}><button className="explorer-file" type="button" onClick={() => onSelect(entry.path)} title={entry.path}>
          <File size={14} /><span>{entry.name}</span>
        </button></li>)}
    {!data.entries.length && <li className="explorer-hint">空目录</li>}
    {data.truncated && <li className="explorer-hint">目录过大，仅显示前 300 项。</li>}
  </ul>;
}

function DirectoryItem({ entry, onSelect }: { entry: Entry; onSelect: (path: string) => void }) {
  const [open, setOpen] = useState(false);
  return <li><button className="explorer-file" type="button" aria-expanded={open} onClick={() => setOpen(!open)} title={entry.path}>
    <ChevronRight size={12} style={{ transform: open ? 'rotate(90deg)' : undefined }} /><Folder size={14} /><span>{entry.name}</span>
  </button>{open && <Directory path={entry.path} onSelect={onSelect} />}</li>;
}

export function ProjectExplorer({ projectRoot }: { projectRoot?: string | null }) {
  const [version, setVersion] = useState(0);
  const [selected, setSelected] = useState('');
  const name = projectRoot?.split(/[\\/]/).filter(Boolean).pop();
  return <section className="project-explorer" aria-label="工程目录树">
    <div className="explorer-heading"><h2>{name ?? '工程文件'}</h2>
      <button type="button" title="刷新工程目录" aria-label="刷新工程目录" onClick={() => setVersion(version + 1)}><RefreshCw size={14} /></button>
    </div>
    <Directory key={`${projectRoot}:${version}`} path="" onSelect={setSelected} />
    {selected && <p className="explorer-selection" title={selected}>{selected}</p>}
  </section>;
}
