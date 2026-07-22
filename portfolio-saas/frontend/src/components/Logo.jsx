// Lattice brand mark: an ascending lattice of connected nodes. Pure inline SVG
// so it scales crisply at any size and inherits the page accent color. Used in
// the top bar and on the login card.
export default function Logo({ size = 24, title = "Lattice" }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      role="img"
      aria-label={title}
    >
      {/* Three ascending node tiers joined into a lattice — portfolios layered
          upward, the product's core metaphor. */}
      <g stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
        <line x1="9" y1="24" x2="16" y2="20" />
        <line x1="16" y1="20" x2="23" y2="16" />
        <line x1="9" y1="24" x2="9" y2="16" />
        <line x1="16" y1="20" x2="16" y2="12" />
        <line x1="23" y1="16" x2="23" y2="8" />
        <line x1="9" y1="16" x2="16" y2="12" />
        <line x1="16" y1="12" x2="23" y2="8" />
      </g>
      <g fill="currentColor">
        <circle cx="9" cy="24" r="2.4" />
        <circle cx="16" cy="20" r="2.4" />
        <circle cx="23" cy="16" r="2.4" />
        <circle cx="9" cy="16" r="2.4" />
        <circle cx="16" cy="12" r="2.4" />
        <circle cx="23" cy="8" r="2.4" />
      </g>
    </svg>
  );
}
