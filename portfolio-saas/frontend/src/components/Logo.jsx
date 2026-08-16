// Simple portfolio mark: three ascending bars — readable at small sizes.
export default function Logo({ size = 24, title = "Holdings" }) {
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
      <rect x="5" y="18" width="6" height="10" rx="1.5" fill="currentColor" opacity="0.45" />
      <rect x="13" y="12" width="6" height="16" rx="1.5" fill="currentColor" opacity="0.7" />
      <rect x="21" y="6" width="6" height="22" rx="1.5" fill="currentColor" />
    </svg>
  );
}
