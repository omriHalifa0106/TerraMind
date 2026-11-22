/** Small inline SVG icons (stroke = currentColor). Decorative: hidden from screen readers. */

type P = { className?: string };
const svg = { "aria-hidden": true, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 } as const;

export function Logo({ className }: P) {
  return (
    <svg aria-hidden viewBox="0 0 120 120" className={className}>
      <rect width="120" height="120" rx="28" fill="#123326" />
      <path d="M12 92 L38 54 L56 72 L80 30 L108 92" fill="none" stroke="#F5F6F2" strokeWidth="9" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx="80" cy="30" r="11" fill="#E8A33D" stroke="#F5F6F2" strokeWidth="5" />
    </svg>
  );
}

export const Wordmark = () => (
  <span className="wordmark" dir="ltr">
    Terra<span>Mind</span>
  </span>
);

export const Mountain = ({ className }: P) => (
  <svg {...svg} className={className} strokeLinejoin="round" strokeLinecap="round">
    <path d="M3 20 L9 11 L13 15 L17 7 L21 20" />
  </svg>
);

export const ArrowStart = ({ className }: P) => (
  <svg {...svg} className={className} strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round">
    <path d="M15 6 L9 12 L15 18" />
  </svg>
);

export const Sun = ({ className }: P) => (
  <svg {...svg} className={className} strokeLinecap="round">
    <circle cx="12" cy="12" r="4.5" />
    <path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8" />
  </svg>
);

export const Clock = ({ className }: P) => (
  <svg {...svg} className={className} strokeLinecap="round">
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </svg>
);

export const Check = ({ className }: P) => (
  <svg {...svg} className={className} strokeWidth={3} strokeLinecap="round" strokeLinejoin="round">
    <path d="M5 12.5 L10 17 L19 7.5" />
  </svg>
);

export const Alert = ({ className }: P) => (
  <svg {...svg} className={className} strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 3 L22 20 H2 Z" />
    <path d="M12 10v4M12 17.2v.1" />
  </svg>
);

export const Leaf = ({ className }: P) => (
  <svg {...svg} className={className} strokeLinecap="round" strokeLinejoin="round">
    <path d="M5 19c0-8 5-13 15-14-1 10-6 15-14 15" />
    <path d="M5 19 L13 11" />
  </svg>
);

export const Send = ({ className }: P) => (
  <svg {...svg} className={className} strokeLinecap="round" strokeLinejoin="round">
    <path d="M20 12 L4 4 L7 12 L4 20 Z" />
    <path d="M7 12h6" />
  </svg>
);

export const Chevron = ({ className }: P) => (
  <svg {...svg} className={className} strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round">
    <path d="M6 9 L12 15 L18 9" />
  </svg>
);

export const MapPin = ({ className }: P) => (
  <svg {...svg} className={className} strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 21s-6.5-6-6.5-11a6.5 6.5 0 0 1 13 0c0 5-6.5 11-6.5 11z" />
    <circle cx="12" cy="10" r="2.3" />
  </svg>
);

/** One recognizable icon per agent (avatars in the pipeline). */
export function AgentIcon({ name, className }: P & { name: string }) {
  const common = { ...svg, className, strokeLinecap: "round", strokeLinejoin: "round" } as const;
  switch (name) {
    case "request_analyst": // reads the request
      return (
        <svg {...common}>
          <path d="M4 5h16v11H9l-5 4z" />
          <path d="M8 9h8M8 12.5h5" />
        </svg>
      );
    case "catalog_search": // searches the catalog
      return (
        <svg {...common}>
          <circle cx="10.5" cy="10.5" r="6" />
          <path d="M15 15l5 5" />
        </svg>
      );
    case "enrichment": // safety guard
      return (
        <svg {...common}>
          <path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z" />
          <path d="M8.8 12.2l2.2 2.2 4.2-4.4" />
        </svg>
      );
    case "route_planner": // plans the route
      return (
        <svg {...common}>
          <circle cx="6" cy="18" r="2.2" />
          <circle cx="18" cy="6" r="2.2" />
          <path d="M8 18h6.5a3.5 3.5 0 0 0 0-7h-5a3.5 3.5 0 0 1 0-7H16" />
        </svg>
      );
    default: // presenter writes the answer
      return (
        <svg {...common}>
          <path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z" />
          <path d="M18.5 15.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z" />
        </svg>
      );
  }
}
