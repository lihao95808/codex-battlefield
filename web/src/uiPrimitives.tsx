export function Stat({ label, value, title }: { label: string; value: number | string; title?: string }) {
  return (
    <div className="stat" title={title}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function Section({ title, items, empty }: { title: string; items: string[]; empty: string }) {
  return (
    <section className="detail-section">
      <h3>{title}</h3>
      {items.length > 0 ? (
        <ul>
          {items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : (
        <p className="muted">{empty}</p>
      )}
    </section>
  );
}

export function BusyRunner({ label, compact = false }: { label: string; compact?: boolean }) {
  return (
    <div className={`busy-runner ${compact ? 'compact' : ''}`} role="status" aria-live="polite">
      <span className="busy-spinner" aria-hidden="true" />
      <span className="busy-runner-label">{label}</span>
      <span className="busy-marquee" aria-hidden="true">
        <i />
      </span>
    </div>
  );
}
