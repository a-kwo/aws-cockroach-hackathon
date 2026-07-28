/* The right margin — "prove it."
 *
 * The signature element: retrieval printed as footnotes beside the argument,
 * the way a serious document cites itself.
 *
 * Two things the spec killed here, both because the real data falsified them:
 *
 * 1. Ink density as confidence. 46% of the 108 evidence rows sit below 0.30
 *    similarity, so proportional ink would have rendered most of the product's
 *    receipts at roughly 2:1 contrast — the least readable text in the product
 *    would have been the part that proves it. Every note is full ink.
 *
 * 2. Any printed threshold. Two VERIFIED wins have weaker top evidence (0.064
 *    and 0.157) than the published MISS (0.315), so a strength score does not
 *    predict an outcome and the interface must never imply that it does.
 *
 * What survives is rank order and a plain-language equivalent. The mark never
 * issues a verdict on the evidence; it says how closely the memory matched what
 * the agent was looking for, and nothing more.
 */

import { useEffect, useRef } from "react";
import type { Find } from "../types";
import { money, shortDate } from "../format";

interface Props {
  find: Find | null;
  onClose: () => void;
}

const SOURCE: Record<string, string> = {
  review: "A customer wrote",
  social: "Someone posted locally",
  trend: "Local trend",
  rival_price: "A competitor's price",
  rival_menu: "A competitor's menu",
  owner_upload: "You told us",
};

/** Plain language, never a number alone, and never a judgement. */
function closeness(similarity: number): string {
  if (similarity >= 0.45) return "closely on point";
  if (similarity >= 0.3) return "on point";
  if (similarity >= 0.15) return "loosely related";
  return "distantly related";
}

export function ProofMargin({ find, onClose }: Props) {
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!find) return;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [find, onClose]);

  if (!find) return null;

  return (
    <div className="proof" role="dialog" aria-modal="true" aria-label="What this was built on">
      <button className="proof__scrim" onClick={onClose} aria-label="Close" />

      <div className="proof__panel">
        <header className="proof__head">
          <div>
            <p className="proof__eyebrow head">What this was built on</p>
            <h2 className="proof__title">{find.title}</h2>
          </div>
          <button ref={closeRef} className="proof__close" onClick={onClose}>
            Close
          </button>
        </header>

        <p className="proof__lede">
          Your agent searched everything it remembers about your shop and these{" "}
          <span className="fig">{find.evidence.length}</span> came back, strongest
          match first. Nothing else went into it.
        </p>

        <ol className="notes">
          {find.evidence.map((item, i) => (
            <li key={item.observation_id} className="note">
              <span className="note__num fig" aria-hidden="true">
                {i + 1}
              </span>
              <div className="note__body">
                <p className="note__quote">{item.content}</p>
                <p className="note__meta head">
                  {SOURCE[item.kind] ?? item.kind}
                  {item.subject && <> · {item.subject}</>}
                  {" · "}
                  <span className="fig">{shortDate(item.observed_at)}</span>
                  {" · "}
                  {closeness(item.similarity)}
                </p>
              </div>
            </li>
          ))}
        </ol>

        {find.verdict === "verified" && find.actual_daily_cents !== null && (
          <div className="proof__outcome proof__outcome--verified">
            <p>
              Predicted <span className="fig">{money(find.predicted_daily_cents)}</span> a
              day · earned{" "}
              <strong className="fig">{money(find.actual_daily_cents)}</strong> a day
            </p>
            {find.method && <p className="proof__method">Measured by {find.method}</p>}
          </div>
        )}

        {find.verdict === "miss" && (
          <div className="proof__outcome proof__outcome--miss">
            <p className="proof__outcomeHead">This one was wrong.</p>
            <p>
              {find.note ?? "It did not pay off, and it stays on the record."}
            </p>
            {/* Deliberately does NOT invite a comparison of match strengths.
             * Two of the verified wins have weaker evidence than this. */}
          </div>
        )}
      </div>
    </div>
  );
}
