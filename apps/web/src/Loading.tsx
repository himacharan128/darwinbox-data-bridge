/**
 * Every wait in the console looks the same: a line saying what is happening,
 * and shaped placeholders so the page keeps its layout instead of going blank.
 *
 * A blank panel reads as "broken" rather than "working", which is what made
 * opening a past migration feel like nothing had happened.
 */
export default function Loading({ label, rows = 3 }: { label: string; rows?: number }) {
  return (
    <div className="panel" role="status" aria-live="polite">
      <p className="loading-line"><i className="spinner" aria-hidden="true" />{label}</p>
      <div className="skeleton">
        {Array.from({ length: rows }, (_, i) => <i key={i} />)}
      </div>
    </div>
  );
}
