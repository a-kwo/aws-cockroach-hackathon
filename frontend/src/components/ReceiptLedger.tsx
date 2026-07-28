/* The record, rendered as receipt tape.
 *
 * The signature element. This product's promise is "we show receipts", and
 * receipt tape is a restaurant owner's native document — the thing they already
 * read every night to find out how the day went. Rendering the ledger as tape
 * rather than as a dashboard table means the format itself carries the claim.
 *
 * Misses are printed exactly like wins, in the same tape, in sequence. That is
 * the whole point: a track record you can only read the good half of is not a
 * track record.
 */

import type { Find, Summary } from "../types";
import { money, shortDate } from "../format";

interface Props {
  finds: Find[];
  summary: Summary;
  onShowEvidence: (find: Find) => void;
}

const VERDICT_LABEL: Record<string, string> = {
  verified: "VERIFIED",
  miss: "MISS",
  estimated: "ESTIMATE",
};

export function ReceiptLedger({ finds, summary, onShowEvidence }: Props) {
  const judged = finds
    .filter((f) => f.verdict !== null)
    .sort((a, b) => (a.measured_at ?? "").localeCompare(b.measured_at ?? ""));

  const rate =
    summary.hit_rate === null ? null : Math.round(summary.hit_rate * 100);

  return (
    <section className="ledger">
      <header className="ledger__head">
        <h2 className="ledger__title">The record</h2>
        <p className="ledger__sub">
          Every move we made, and whether it paid. Misses included — that is what
          makes the rest of it worth reading.
        </p>
      </header>

      <div className="tape" role="table" aria-label="Ledger of past moves">
        <div className="tape__edge tape__edge--top" aria-hidden="true" />

        <div className="tape__body">
          <div className="tape__masthead">
            <p className="tape__shop">ROSA&rsquo;S TRATTORIA</p>
            <p className="tape__meta">BRASS TACKS · AUTOPILOT LEDGER</p>
          </div>

          <div className="tape__rule" aria-hidden="true" />

          {judged.length === 0 && (
            <p className="tape__empty">
              Nothing judged yet. The first verdict lands when a move&rsquo;s
              check-in date arrives.
            </p>
          )}

          {judged.map((find) => (
            <article
              key={find.id}
              className={`entry entry--${find.verdict}`}
              role="row"
            >
              <div className="entry__line">
                <span className="entry__date">
                  {find.measured_at ? shortDate(find.measured_at) : ""}
                </span>
                <span className="entry__title">{find.title}</span>
                <span className="entry__amount">
                  {find.verdict === "miss"
                    ? money(0)
                    : money(find.actual_daily_cents ?? 0)}
                </span>
              </div>

              <div className="entry__meta">
                <span className={`stamp stamp--${find.verdict}`}>
                  {VERDICT_LABEL[find.verdict!]}
                </span>
                <span className="entry__predicted">
                  predicted {money(find.predicted_daily_cents)}/day
                </span>
                <button
                  className="entry__evidence"
                  onClick={() => onShowEvidence(find)}
                >
                  {find.evidence.length} sources
                </button>
              </div>

              {find.note && <p className="entry__note">{find.note}</p>}
            </article>
          ))}

          <div className="tape__rule" aria-hidden="true" />

          <div className="tape__totals">
            <div className="tape__totalLine">
              <span>MOVES THAT PAID</span>
              <span>
                {summary.verified} of {summary.judged}
              </span>
            </div>
            <div className="tape__totalLine">
              <span>STILL BEING MEASURED</span>
              <span>{summary.estimated}</span>
            </div>
            <div className="tape__totalLine tape__totalLine--strong">
              <span>EARNING NOW</span>
              <span>{money(summary.verified_daily_cents)} / day</span>
            </div>
          </div>

          {rate !== null && (
            <p className="tape__rate">
              <strong>{rate}%</strong> hit rate
            </p>
          )}

          <p className="tape__foot">
            Every dollar above is traced to a prediction made before the fact.
          </p>
        </div>

        <div className="tape__edge tape__edge--bottom" aria-hidden="true" />
      </div>
    </section>
  );
}
