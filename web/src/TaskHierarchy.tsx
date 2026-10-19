import { useMemo, useState } from 'react';
import Tree from 'rc-tree';
import type { DataNode } from 'rc-tree/lib/interface';
import 'rc-tree/assets/index.css';
import type { BattlefieldTask } from './types';
import { statusLabel } from './labels';

export function TaskHierarchy({ tasks, selectedId, collapsedIds, depthLimit, onSelect, onToggle, onExpandAll, onCollapse }: {
  tasks: BattlefieldTask[]; selectedId: string | null; collapsedIds: Set<string>; depthLimit: number | null;
  onSelect: (id: string) => void; onToggle: (task: BattlefieldTask) => void;
  onExpandAll: () => void; onCollapse: () => void;
}) {
  const [search, setSearch] = useState('');
  const { data, byId } = useMemo(() => {
    const byId = new Map(tasks.map((task) => [task.id, task]));
    const nodes = new Map<string, DataNode>(tasks.map((task) => [task.id, {
      key: task.id, title: <span className="task-hierarchy-title" title={`${task.title}\n${statusLabel(task.status)}`}>
        <span className={`task-hierarchy-status ${task.status}`} /><span>{task.title}</span><small>{statusLabel(task.status)}</small>
      </span>, children: [],
    }]));
    const data: DataNode[] = [];
    for (const task of tasks) {
      const node = nodes.get(task.id)!;
      // Malformed cycles must not silently swallow the entire hierarchy.
      let current: BattlefieldTask | undefined = task;
      const seen = new Set<string>();
      while (current?.parent_id && !seen.has(current.id)) { seen.add(current.id); current = byId.get(current.parent_id); }
      const cyclic = current !== undefined && seen.has(current.id);
      const parent = task.parent_id ? nodes.get(task.parent_id) : null;
      if (parent && !cyclic) parent.children!.push(node); else data.push(node);
    }
    return { data, byId };
  }, [tasks]);
  const expandedKeys = tasks.filter((task) => !collapsedIds.has(task.id) && (depthLimit === null || task.depth < depthLimit)).map((task) => task.id);
  return <section className="project-explorer task-hierarchy" aria-label="当前任务层级树">
    <div className="explorer-heading"><h2>任务层级 · {tasks.length}</h2><div>
      <button type="button" onClick={onExpandAll}>展开</button><button type="button" onClick={onCollapse}>收起一层</button>
    </div></div>
    <input aria-label="搜索任务节点" placeholder="搜索任务节点…" value={search} onChange={(e) => setSearch(e.target.value)} />
    {search.trim() && <div className="task-search-results">{tasks.filter((t) => `${t.title} ${t.id}`.toLowerCase().includes(search.toLowerCase())).slice(0, 100).map((task) =>
      <button type="button" key={task.id} className={selectedId === task.id ? 'selected' : ''} onClick={() => onSelect(task.id)}>{task.title}</button>)}</div>}
    <Tree treeData={data} selectedKeys={selectedId ? [selectedId] : []} expandedKeys={expandedKeys}
      autoExpandParent={false} showLine showIcon={false} virtual height={320} itemHeight={30}
      onSelect={(keys) => { if (keys[0] !== undefined) onSelect(String(keys[0])); }}
      onExpand={(_, info) => { const task = byId.get(String(info.node.key)); if (task) onToggle(task); }} />
    {!tasks.length && <p className="explorer-hint">尚未生成任务树。</p>}
  </section>;
}
