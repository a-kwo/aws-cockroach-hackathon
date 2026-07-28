/* A verdict, carried by three channels at once.
 *
 * The palette validator found that the four semantic colours together FAIL CVD
 * separation as one categorical channel — no design direction had caught it. So
 * colour is never the only signal. Every verdict also carries:
 *
 *   verified        filled disc
 *   miss            open square with a diagonal strike
 *   measuring       ring closed by a dotted arc
 *   waiting on you  outlined diamond with 45-degree hatch
 *
 * ...and always the word itself. A reader who sees no colour at all still gets
 * the verdict from the shape and the label.
 */

export type MarkKind = "verified" | "miss" | "measuring" | "waiting";

const LABEL: Record<MarkKind, string> = {
  verified: "Verified",
  miss: "Miss",
  measuring: "Measuring",
  waiting: "Waiting on you",
};

function Glyph({ kind }: { kind: MarkKind }) {
  const common = { width: 12, height: 12, viewBox: "0 0 12 12", "aria-hidden": true as const };

  if (kind === "verified") {
    return (
      <svg {...common}>
        <circle cx="6" cy="6" r="5" fill="currentColor" />
      </svg>
    );
  }
  if (kind === "miss") {
    return (
      <svg {...common}>
        <rect x="1.2" y="1.2" width="9.6" height="9.6" fill="none" stroke="currentColor" strokeWidth="1.6" />
        <line x1="1.8" y1="10.2" x2="10.2" y2="1.8" stroke="currentColor" strokeWidth="1.6" />
      </svg>
    );
  }
  if (kind === "measuring") {
    return (
      <svg {...common}>
        <circle cx="6" cy="6" r="4.6" fill="none" stroke="currentColor" strokeWidth="1.6" strokeDasharray="2.2 1.8" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <defs>
        <pattern id="hatch45" width="3" height="3" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
          <line x1="0" y1="0" x2="0" y2="3" stroke="currentColor" strokeWidth="1.1" />
        </pattern>
      </defs>
      <rect x="6" y="0.6" width="7.6" height="7.6" transform="rotate(45 6 0.6)" fill="url(#hatch45)" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  );
}

export function VerdictMark({
  kind,
  children,
}: {
  kind: MarkKind;
  children?: React.ReactNode;
}) {
  return (
    <span className={`mark mark--${kind}`}>
      <Glyph kind={kind} />
      <span className="mark__label">{children ?? LABEL[kind]}</span>
    </span>
  );
}

export function markFor(verdict: string | null, status: string): MarkKind {
  if (verdict === "verified") return "verified";
  if (verdict === "miss") return "miss";
  if (verdict === "estimated") return "measuring";
  return status === "later" ? "waiting" : "waiting";
}
