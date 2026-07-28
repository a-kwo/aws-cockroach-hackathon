/* The sheet. Section 7 of the spec — the part that had to be right.
 *
 * Render order, and the reason for it:
 *
 *   1 kicker      issue number, and what the find was drawn from
 *   2 title       whole, never truncated
 *   3 THE ASK     money, when it gets checked, and THE BUTTONS
 *   4 lede        the clause before the first enumerator, at 21px
 *   5 the work    "Four things I'll draft. One is yours." + steps
 *   6 the guards  his own limits, verbatim, lifted out of line nine
 *   7 the why     rationale, clamped
 *   8 the proof   count of memories, opens the margin
 *   9 as written  the untouched text, with a word count
 *  10 byline      which agents ran, and when
 *
 * The buttons are element 3, above every paragraph. Kicker + title + ask + lede
 * + work header is roughly forty words, so she can decide without scrolling into
 * prose at all. Elements 5-10 are the thirty extra seconds she may choose to
 * spend, not a toll she must pay.
 */

import { useState } from "react";
import type { Find, Summary } from "../types";
import { parseMove, workloadHeadline } from "../prose";
import { money, perMonth, shortDate } from "../format";

interface Props {
  find: Find;
  summary: Summary;
  issueNumber: number;
  onDecide: (findId: string, status: "accepted" | "later" | "rejected") => void;
  onShowProof: (find: Find) => void;
}

function sourceSummary(find: Find): string {
  const counts = new Map<string, number>();
  for (const e of find.evidence) {
    const label =
      e.kind === "review" ? "reviews"
      : e.kind === "social" ? "forum posts"
      : e.kind === "trend" ? "local trends"
      : "competitor notes";
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts].map(([label, n]) => `${n} ${label}`).join(" and ");
}

export function FindSheet({ find, summary, issueNumber, onDecide, onShowProof }: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [showAll, setShowAll] = useState(false);

  const parsed = parseMove(find.move);
  const headline = workloadHeadline(parsed.steps);

  const toggle = (key: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  return (
    <article className="sheet">
      {/* 1 — kicker */}
      <p className="sheet__kicker head">
        <span className="fig">No. {issueNumber}</span>
        <span className="sheet__kickerDot" aria-hidden="true">·</span>
        From {sourceSummary(find)}
      </p>

      {/* 2 — title, whole */}
      <h1 className="sheet__title">{find.title}</h1>

      {/* 3 — THE ASK. Above the prose, deliberately. */}
      <div className="ask">
        <div className="ask__money">
          <span className="ask__figure fig">{money(find.predicted_daily_cents)}</span>
          <span className="ask__unit">a day</span>
          <span className="ask__month">
            {perMonth(find.predicted_daily_cents)} a month if it holds
          </span>
        </div>

        <p className="ask__check">
          I&rsquo;ll check it against your sales on{" "}
          <span className="fig">{shortDate(find.verify_after)}</span> and tell you
          either way.
        </p>

        <div className="ask__record">
          {summary.hit_rate === null ? (
            <>No calls judged yet. This one will be.</>
          ) : (
            <>
              <strong className="fig">
                {summary.verified} of {summary.judged}
              </strong>{" "}
              calls right so far
              {summary.miss > 0 && <> · {summary.miss} wrong, still on the record</>}
            </>
          )}
        </div>

        <div className="ask__actions">
          <button className="btn btn--yes" onClick={() => onDecide(find.id, "accepted")}>
            Yes — draft it for me
          </button>
          <button className="btn btn--no" onClick={() => onDecide(find.id, "rejected")}>
            Not this one
          </button>
          <button className="btn btn--later" onClick={() => onDecide(find.id, "later")}>
            Ask me again next week
          </button>
        </div>

        <p className="ask__promise">Nothing runs until you say yes.</p>
      </div>

      {/* 4 — the lede */}
      {parsed.lede && <p className="sheet__lede">{parsed.lede}</p>}

      {/* 5 — the work, with her workload priced in the header */}
      {parsed.steps.length > 0 && (
        <section className="work">
          <h2 className="work__head head">{headline}</h2>
          <ol className="work__list">
            {parsed.steps.map((step, i) => {
              const key = `${i}`;
              const open = expanded.has(key);
              return (
                <li key={key} className={`step step--${step.owner}`}>
                  <span className="step__owner head" aria-label={step.owner === "owner" ? "Yours" : "Mine"}>
                    {step.owner === "owner" ? "YOURS" : "MINE"}
                  </span>
                  <div className="step__body">
                    <p className={`step__text${open ? "" : " is-clamped"}`}>{step.text}</p>
                    {step.text.length > 130 && (
                      <button className="step__more" onClick={() => toggle(key)}>
                        {open ? "Less" : "More"}
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        </section>
      )}

      {/* 6 — the guards, verbatim, lifted out of the paragraph */}
      {parsed.guards.length > 0 && (
        <section className="guards">
          <h2 className="guards__head head">What I won&rsquo;t do without you</h2>
          <ul className="guards__list">
            {parsed.guards.map((g, i) => (
              <li key={i}>{g}</li>
            ))}
          </ul>
        </section>
      )}

      {/* 7 — the why */}
      <section className="why">
        <h2 className="why__head head">Why I think so</h2>
        <p className={`why__text${showAll ? "" : " is-clamped"}`}>{find.rationale}</p>
        <button className="why__more" onClick={() => setShowAll((v) => !v)}>
          {showAll ? "Show less" : "Show the whole argument"}
        </button>
      </section>

      {/* 8 — the proof */}
      <button className="proofLink" onClick={() => onShowProof(find)}>
        <span className="fig">{find.evidence.length}</span> memories, strongest first
        <span className="proofLink__arrow" aria-hidden="true"> →</span>
      </button>

      {/* 9 — as written. The clamp above is CSS, so this is the same text,
           not a different version of it. */}
      {!parsed.isShort && (
        <details className="asWritten">
          <summary>
            Read it as written · <span className="fig">{parsed.wordCount}</span> words
          </summary>
          <p className="asWritten__body">{find.move}</p>
        </details>
      )}

      {/* 10 — byline */}
      <p className="sheet__byline head">
        Radar, Analyst and Meter ran overnight · filed{" "}
        <span className="fig">{shortDate(find.created_at)}</span>
      </p>
    </article>
  );
}
