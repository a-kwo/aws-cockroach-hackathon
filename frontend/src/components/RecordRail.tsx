/* The left rail — "should I believe this?"
 *
 * Lives in what used to be dead margin. The record is here rather than on a
 * separate tab because the track record is the answer to the question she is
 * asking while she reads the ask, and putting it a click away wastes the trust
 * the ledger spent weeks earning.
 *
 * The hit rate is printed as "6 of 7 calls right", never as a bare percentage:
 * 86% of seven is a number pretending to a precision it does not have.
 */

import type { Find, Summary } from "../types";
import { money, shortDate } from "../format";
import { VerdictMark, markFor } from "./VerdictMark";

interface Props {
  finds: Find[];
  summary: Summary;
  corpusCount: number;
  onShowProof: (find: Find) => void;
}

export function RecordRail({ finds, summary, corpusCount, onShowProof }: Props) {
  const judged = finds
    .filter((f) => f.verdict !== null)
    .sort((a, b) => (b.measured_at ?? "").localeCompare(a.measured_at ?? ""));

  return (
    <aside className="rail" aria-label="The record">
      <div className="rail__block">
        <h2 className="rail__head head">Earning now</h2>
        <p className="rail__figure fig">{money(summary.verified_daily_cents)}</p>
        <p className="rail__unit">a day, checked against real sales</p>
      </div>

      <div className="rail__block">
        <h2 className="rail__head head">The record</h2>
        <p className="rail__record">
          {summary.hit_rate === null ? (
            "Nothing judged yet."
          ) : (
            <>
              <strong className="fig">
                {summary.verified} of {summary.judged}
              </strong>{" "}
              calls right
            </>
          )}
        </p>
        <ul className="rail__tally">
          <li>
            <VerdictMark kind="verified">{summary.verified} paid off</VerdictMark>
          </li>
          <li>
            <VerdictMark kind="miss">{summary.miss} missed</VerdictMark>
          </li>
          <li>
            <VerdictMark kind="measuring">{summary.estimated} measuring</VerdictMark>
          </li>
        </ul>
      </div>

      <div className="rail__block rail__block--grow">
        <h2 className="rail__head head">Every call, in order</h2>
        <ol className="ledger">
          {judged.map((find) => {
            const kind = markFor(find.verdict, find.status);
            return (
              <li key={find.id} className={`ledgerRow ledgerRow--${kind}`}>
                <button className="ledgerRow__btn" onClick={() => onShowProof(find)}>
                  <span className="ledgerRow__top">
                    <VerdictMark kind={kind} />
                    <span className="ledgerRow__amount fig">
                      {find.verdict === "miss"
                        ? money(0)
                        : money(find.actual_daily_cents ?? 0)}
                    </span>
                  </span>
                  <span className="ledgerRow__title">{find.title}</span>
                  <span className="ledgerRow__meta">
                    <span className="fig">{shortDate(find.measured_at!)}</span>
                    {" · predicted "}
                    <span className="fig">{money(find.predicted_daily_cents)}</span>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      </div>

      <p className="rail__foot">
        <span className="fig">{corpusCount}</span> things remembered about your shop.
        Every one still searchable.
      </p>
    </aside>
  );
}
