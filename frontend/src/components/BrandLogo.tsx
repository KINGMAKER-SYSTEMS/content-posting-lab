// ═══════════════════════════════════════════════════════════════════════
// BrandLogo — inline Rising Tides horizontal logo.
//
// Uses `currentColor` as fill so the logo naturally adapts to the current
// theme's --foreground token. No separate light/dark files needed.
//
// Source: frontend/src/assets/brand/logos/logo-horizontal-white.svg
// (identical path + text geometry, fill swapped for currentColor)
// ═══════════════════════════════════════════════════════════════════════

interface BrandLogoProps {
  className?: string;
  title?: string;
}

export function BrandLogo({ className, title = 'Rising Tides Entertainment' }: BrandLogoProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 359.7 337.5"
      role="img"
      aria-label={title}
      className={className}
      fill="currentColor"
    >
      <text
        fill="currentColor"
        fontFamily="Poppins, Helvetica, Arial, sans-serif"
        fontSize="70"
        fontWeight="700"
        letterSpacing="1.4"
        transform="translate(0 319.3)"
      >
        <tspan x="0" y="0">RISING TIDES</tspan>
      </text>
      <path
        fill="currentColor"
        d="M206.1,173.2c3.8-33.2,61.7-25.2,111.6-35.3,0,0-43.7-34.5-82.4-88.2C180.5-26.4,115.2,7.5,115.2,7.5c0,0,41.5,17.1,38.3,44.6-3.8,33.2-61.7,25.2-111.6,35.3,0,0,43.7,34.5,82.4,88.2,54.8,76.1,120,42.2,120,42.2,0,0-41.5-17.1-38.3-44.6Z"
      />
    </svg>
  );
}
