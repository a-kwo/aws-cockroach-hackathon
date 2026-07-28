/* The 7am screen.
 *
 * An owner opens this with about ninety seconds before prep starts. Its single
 * job is to get one good decision made, with enough warrant to make it
 * confidently.
 *
 * Two deliberate departures from the earlier mock:
 *
 * 1. The hero is the decision, not a running money total. A total is a vanity
 *    metric at 7am — it does not need her. The find does.
 * 2. The track record sits inside the ask, not on another screen. "6 of 7 paid
 *    off" is what makes "+$23 a day" believable, and separating the claim from
 *    its warrant wastes the trust the ledger has earned.
 */

import type { Find, Summary } from "../types";
import {
  confidenceWords,
  money,
  perMonth,
  relativeDays,
} from "../format";

interface Props {
  find: Find | undefined;
  summary: Summary;
  queued: number;
  onDecide: (findId: string, status: "accepted" | "later" | "rejected") => void;
  onShowEvidence: (find: Find) => void;
}

export function MorningDecision({
  find,
  summary,
  queued,
  onDecide,
  onShowEvidence,
}: Props) {
  if (!find) {
    return (
      <section className="decision decision--empty">
        <p className="decision__eyebrow">This morning</p>
        <h1 className="decision__done">Nothing needs you.</h1>
        <p className="decision__doneSub">
          The agents ran overnight and found nothing worth interrupting you for.
          Go run your shop.
        </p>
      </section>
    );
  }

  const rate =
    summary.hit_rate === null ? null : Math.round(summary.hit_rate * 100);

  return (
    <section className="decision">
      <header className="decision__head">
        <p className="decision__eyebrow">
          This morning
          {queued > 1 && (
            <span className="decision__queue">
              {queued - 1} more after this
            </span>
          )}
        </p>
      </header>

      <div className="decision__card">
        <div className="decision__value">
          <span className="decision__emoji" aria-hidden="true">
            {find.emoji ?? "💡"}
          </span>
          <div>
            <p className="decision__amount">
              {money(find.predicted_daily_cents)}
              <span className="decision__per">a day</span>
            </p>
            <p className="decision__monthly">
              about {perMonth(find.predicted_daily_cents)} a month, every month
            </p>
          </div>
        </div>

        <h1 className="decision__title">{find.title}</h1>

        {find.move && <p className="decision__move">{find.move}</p>}

        <p className="decision__why">{find.rationale}</p>

        {/* The warrant. Deliberately adjacent to the ask rather than on the
         * ledger screen: this is the sentence that makes the number credible. */}
        <div className="warrant">
          <p className="warrant__record">
            {rate === null ? (
              <>No moves judged yet — this one will be.</>
            ) : (
              <>
                <strong>
                  {summary.verified} of {summary.judged}
                </strong>{" "}
                past moves paid off.{" "}
                {summary.miss > 0 && (
                  <>
                    {summary.miss} didn&rsquo;t, and {summary.miss === 1 ? "it's" : "they're"}{" "}
                    still on the record.
                  </>
                )}
              </>
            )}
          </p>
          <p className="warrant__check">
            {confidenceWords(find.confidence)}. We&rsquo;ll check{" "}
            {relativeDays(find.verify_after)} and tell you either way.
          </p>
        </div>

        <button
          className="decision__evidence"
          onClick={() => onShowEvidence(find)}
        >
          Show me why — {find.evidence.length} things we read
        </button>
      </div>

      <div className="decision__actions">
        <button
          className="btn btn--primary"
          onClick={() => onDecide(find.id, "accepted")}
        >
          Do it
        </button>
        <button
          className="btn btn--ghost"
          onClick={() => onDecide(find.id, "later")}
        >
          Later
        </button>
        <button
          className="btn btn--quiet"
          onClick={() => onDecide(find.id, "rejected")}
        >
          Not for us
        </button>
      </div>
    </section>
  );
}
