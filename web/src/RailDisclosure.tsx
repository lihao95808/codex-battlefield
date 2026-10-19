import { useState, type ReactNode } from 'react';

/** 初次展开才挂载；收起后保留，避免丢失输入中的表单或对话。 */
export function RailDisclosure({ title, hint, children, forceOpen = false, keepMounted = false }: {
  title: string;
  hint?: string;
  children: ReactNode;
  forceOpen?: boolean;
  keepMounted?: boolean;
}) {
  const [visited, setVisited] = useState(false);
  return (
    <details className="rail-disclosure" open={forceOpen ? true : undefined} onToggle={(event) => {
      if (event.currentTarget.open) setVisited(true);
    }}>
      <summary><span>{title}</span>{hint && <small>{hint}</small>}</summary>
      {(visited || forceOpen || keepMounted) && <div className="rail-disclosure-body">{children}</div>}
    </details>
  );
}
