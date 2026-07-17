// A dependency-free sparkline: a polyline scaled into a viewBox. Takes a flat
// array of numbers (oldest -> newest). ~40 lines, no recharts.
export default function Sparkline({ data, width = 200, height = 48, className = "" }) {
  const pts = Array.isArray(data) ? data.filter((n) => Number.isFinite(Number(n))) : [];
  if (pts.length < 2) {
    return <div className={`sparkline empty ${className}`}>—</div>;
  }
  const nums = pts.map(Number);
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1; // avoid divide-by-zero on a flat line
  const stepX = width / (nums.length - 1);
  const y = (v) => height - ((v - min) / span) * height;
  const points = nums.map((v, i) => `${(i * stepX).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg
      className={`sparkline ${className}`}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
    >
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="2" />
    </svg>
  );
}
