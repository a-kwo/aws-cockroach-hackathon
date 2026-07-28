/* Why we said that.
 *
 * The answer to the only question a skeptical owner actually asks: how do you
 * know? Each row is an observation the vector search returned when the Analyst
 * was reasoning, with the retrieval score that ranked it.
 *
 * These are not illustrative. They are the exact rows stored in find_evidence at
 * the moment the recommendation was written, which is why a find cannot be
 * created without them.
 *
 * Deliberately a drawer, not a screen: a trusting owner never opens it, and a
 * skeptical one is one tap away. Neither pays for the other's needs.
 */

import { useEffect, useRef } from "react";
import type { Find } from "../types";
import { money, shortDate, similarityPercent } from "../format";

interface Props {
  find: Find | null;
  onClose: () => void;
}

const KIND_LABEL: Record<string, string> = {
  review: "customer review",
  social: "local forum",
  trend: "local trend",
  rival_price: "competitor price",
  rival_menu: "competitor menu",
  owner_upload: "you told us",
};

export function EvidenceTrail({ find, onClose }: Props) {
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!find) return;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [find, onClose]);

  if (!find) return null;

  return (
    <div className="drawer" role="dialog" aria-modal="true" aria-label="Why we said that">
      <button className="drawer__scrim" onClick={onClose} aria-label="Close" />

      <div className="drawer__panel">
        <header className="drawer__head">
          <div>
            <p className="drawer__eyebrow">Why we said that</p>
            <h2 className="drawer__title">{find.title}</h2>
          </div>
          <button ref={closeRef} className="drawer__close" onClick={onClose}>
            Close
          </button>
        </header>

        <p className="drawer__lede">
          Your agent searched everything it has ever read about your business and
          found these {find.evidence.length}. They are what the recommendation was
          built on — nothing else.
        </p>

        <ol className="evidence">
          {find.evidence.map((item) => (
            <li key={item.observation_id} className="evidence__item">
              <div className="evidence__bar" aria-hidden="true">
                <span
                  className="evidence__fill"
                  style={{ width: `${Math.max(4, item.similarity * 100)}%` }}
                />
              </div>

              <div className="evidence__body">
                <p className="evidence__content">&ldquo;{item.content}&rdquo;</p>
                <p className="evidence__meta">
                  <span className="evidence__kind">
                    {KIND_LABEL[item.kind] ?? item.kind}
                    {item.subject && ` · ${item.subject}`}
                  </span>
                  <span className="evidence__dot" aria-hidden="true">
                    ·
                  </span>
                  <span>{shortDate(item.observed_at)}</span>
                  <span className="evidence__dot" aria-hidden="true">
                    ·
                  </span>
                  <span
                    className="evidence__match"
                    title="How closely this matched what the agent was looking for"
                  >
                    {similarityPercent(item.similarity)} match
                  </span>
                </p>
              </div>
            </li>
          ))}
        </ol>

        {/* When a find missed, this drawer is the most honest screen in the
         * product: the thin evidence is visible, and it explains itself. */}
        {find.verdict === "miss" && (
          <div className="drawer__lesson">
            <p className="drawer__lessonTitle">This one missed.</p>
            <p>
              {find.note ??
                "It did not pay off, and it stays on the record."}{" "}
              Compare the match scores above with a move that worked — thin
              evidence is usually why.
            </p>
          </div>
        )}

        {find.verdict === "verified" && find.actual_daily_cents !== null && (
          <div className="drawer__outcome">
            <p>
              Predicted {money(find.predicted_daily_cents)}/day · earned{" "}
              <strong>{money(find.actual_daily_cents)}/day</strong>
            </p>
            {find.method && <p className="drawer__method">Measured by: {find.method}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
