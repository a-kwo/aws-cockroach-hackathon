/* The road to the goal — a ruler, not a chart.
 *
 * Two grafts from losing directions live here:
 *
 * 1. The gap is hatched AND LABELLED, so empty space on the ruler reads as the
 *    remaining job rather than as absent data. That was the observed failure in
 *    the previous build, where the actionable gold segment was the smallest and
 *    least visible part of the bar.
 *
 * 2. The cumulative growth chart is REFUSED rather than restyled. Two months of
 *    ledger cannot carry a trend line, and drawing one anyway would be the
 *    opposite of a product that publishes its misses. One line of type and a
 *    2/6 counter say so out loud, which turns an absence into an argument and
 *    gives the team a dated moment to ship the line honestly later.
 */

import { useState } from "react";
import type { Find } from "../types";
import { money } from "../format";
import { VerdictMark } from "./VerdictMark";

interface Props {
  realizedDailyCents: number;
  openFinds: Find[];
  goalMonthlyCents: number | null;
  monthsOfLedger: number;
  onShowProof: (find: Find) => void;
}

const PER_MONTH = 30;
const MONTHS_A_LINE_NEEDS = 6;

export function Road({
  realizedDailyCents,
  openFinds,
  goalMonthlyCents,
  monthsOfLedger,
  onShowProof,
}: Props) {
  const [hovered, setHovered] = useState<string | null>(null);
  if (!goalMonthlyCents) return null;

  const realized = realizedDailyCents * PER_MONTH;
  const waiting = openFinds.reduce(
    (sum, f) => sum + f.predicted_daily_cents * PER_MONTH,
    0,
  );
  const gap = Math.max(0, goalMonthlyCents - realized - waiting);
  const scale = Math.max(goalMonthlyCents, realized + waiting);
  const pct = (cents: number) => (cents / scale) * 100;

  return (
    <article className="sheet road">
      <p className="sheet__kicker head">The road</p>
      <h2 className="road__title">
        {money(realized)} of {money(goalMonthlyCents)} a month
      </h2>

      <div className="ruler" role="img"
        aria-label={`${money(realized)} earning now, ${money(waiting)} waiting on your decision, ${money(gap)} still to find, against a goal of ${money(goalMonthlyCents)} a month`}>
        <span
          className="ruler__seg ruler__seg--earning"
          style={{ width: `${pct(realized)}%` }}
          onMouseEnter={() => setHovered("earning")}
          onMouseLeave={() => setHovered(null)}
        />
        {openFinds.map((f) => (
          <span
            key={f.id}
            className={`ruler__seg ruler__seg--waiting${hovered === f.id ? " is-hot" : ""}`}
            style={{ width: `${pct(f.predicted_daily_cents * PER_MONTH)}%` }}
            onMouseEnter={() => setHovered(f.id)}
            onMouseLeave={() => setHovered(null)}
          />
        ))}
        {gap > 0 && (
          <span className="ruler__seg ruler__seg--gap" style={{ width: `${pct(gap)}%` }} />
        )}
      </div>

      <p className="ruler__readout" aria-live="polite">
        {hovered === "earning" ? (
          <>
            <strong className="fig">{money(realized)}</strong> a month, already
            checked against your sales.
          </>
        ) : hovered ? (
          (() => {
            const f = openFinds.find((x) => x.id === hovered);
            return f ? (
              <>
                <strong className="fig">
                  {money(f.predicted_daily_cents * PER_MONTH)}
                </strong>{" "}
                a month — {f.title}
              </>
            ) : null;
          })()
        ) : (
          <>
            <span className="fig">{money(gap)}</span> a month still to find ·{" "}
            <span className="fig">{openFinds.length}</span> waiting on you
          </>
        )}
      </p>

      <ul className="ruler__key">
        <li>
          <VerdictMark kind="verified">Earning · {money(realized)}</VerdictMark>
        </li>
        <li>
          <VerdictMark kind="waiting">Waiting on you · {money(waiting)}</VerdictMark>
        </li>
      </ul>

      {/* The refusal, stated rather than hidden. */}
      <p className="road__refusal">
        <span className="road__counter fig">
          {monthsOfLedger} / {MONTHS_A_LINE_NEEDS}
        </span>
        {monthsOfLedger} months of ledger. A trend line needs {MONTHS_A_LINE_NEEDS}.
        We&rsquo;ll draw one when there is one.
      </p>

      {openFinds.length > 0 && (
        <ol className="stops">
          {openFinds.map((f) => (
            <li key={f.id} className="stop">
              <span className="stop__money fig">
                {money(f.predicted_daily_cents * PER_MONTH)}
              </span>
              <div className="stop__body">
                <p className="stop__title">{f.title}</p>
                <button className="stop__proof" onClick={() => onShowProof(f)}>
                  <span className="fig">{f.evidence.length}</span> memories →
                </button>
              </div>
              {f.status === "later" && (
                <span className="stop__flag head">Asked again later</span>
              )}
            </li>
          ))}
        </ol>
      )}
    </article>
  );
}
