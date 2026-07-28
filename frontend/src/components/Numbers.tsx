/* The numbers behind the record.
 *
 * Three questions an owner actually asks, each answered by the form that fits:
 *
 * - "Is this growing?"        change over time  -> cumulative bars vs goal pace
 * - "What did it get right?"  small counts      -> stat tiles, not a pie chart
 * - "What is it reading?"     magnitude by name -> a ranked bar
 *
 * A table view accompanies the growth chart so the data is reachable without
 * relying on colour or shape.
 */

import { useState } from "react";
import type { DemoData } from "../types";
import { money } from "../format";

interface Props {
  data: DemoData;
}

const PER_MONTH = 30;

function monthLabel(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", { month: "short" });
}

export function Numbers({ data }: Props) {
  const [showTable, setShowTable] = useState(false);
  const [hovered, setHovered] = useState<number | null>(null);

  // Cumulative: each month's verified finds keep earning, so the running daily
  // rate is the honest picture of compounding rather than per-month deltas.
  let running = 0;
  const series = data.monthly.map((m) => {
    running += m.verified_daily_cents;
    return {
      month: monthLabel(m.month),
      dailyCents: running,
      monthlyCents: running * PER_MONTH,
      verified: m.verified,
      miss: m.miss,
    };
  });

  const goal = data.business.goal_monthly_cents ?? 0;
  const ceiling = Math.max(goal, ...series.map((s) => s.monthlyCents), 1);
  const kindMax = Math.max(...data.kinds.map((k) => k.count), 1);

  const KIND_LABEL: Record<string, string> = {
    review: "Customer reviews",
    trend: "Local trends",
    rival_price: "Competitor prices",
    rival_menu: "Competitor menus",
    social: "Forum mentions",
    owner_upload: "Things you told us",
  };

  return (
    <section className="numbers">
      {/* ---- Growth ---- */}
      <div className="panel">
        <div className="panel__head">
          <h2 className="panel__title">What&rsquo;s earning, month by month</h2>
          <button className="panel__toggle" onClick={() => setShowTable((v) => !v)}>
            {showTable ? "Show chart" : "Show table"}
          </button>
        </div>
        <p className="panel__sub">
          Running total of moves that were checked and paid off. Verified only —
          estimates are not counted until they are measured.
        </p>

        {showTable ? (
          <table className="dtable">
            <caption className="visually-hidden">
              Monthly running total of verified earnings
            </caption>
            <thead>
              <tr>
                <th scope="col">Month</th>
                <th scope="col">Earning / month</th>
                <th scope="col">Verified</th>
                <th scope="col">Missed</th>
              </tr>
            </thead>
            <tbody>
              {series.map((s) => (
                <tr key={s.month}>
                  <th scope="row">{s.month}</th>
                  <td>{money(s.monthlyCents)}</td>
                  <td>{s.verified}</td>
                  <td>{s.miss}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="growth">
            <div className="growth__plot">
              <div
                className="growth__goal"
                style={{ bottom: `${(goal / ceiling) * 100}%` }}
              >
                <span>goal · {money(goal)}</span>
              </div>
              {series.map((s, i) => (
                <div
                  key={s.month}
                  className="growth__col"
                  onMouseEnter={() => setHovered(i)}
                  onMouseLeave={() => setHovered(null)}
                >
                  <span className="growth__value">{money(s.monthlyCents)}</span>
                  <div
                    className={`growth__bar${hovered === i ? " is-hovered" : ""}`}
                    style={{ height: `${(s.monthlyCents / ceiling) * 100}%` }}
                  />
                  <span className="growth__label">{s.month}</span>
                </div>
              ))}
            </div>
            <p className="growth__caption" aria-live="polite">
              {hovered === null
                ? "Solid bars are money that was checked against real outcomes."
                : `${series[hovered].month}: ${series[hovered].verified} verified, ${series[hovered].miss} missed.`}
            </p>
          </div>
        )}
      </div>

      {/* ---- Verdicts: counts this small are tiles, not a chart ---- */}
      <div className="panel">
        <h2 className="panel__title">Every move, scored</h2>
        <div className="tiles">
          <div className="tile tile--verified">
            <p className="tile__num">{data.summary.verified}</p>
            <p className="tile__label">Paid off</p>
            <p className="tile__note">Checked against real outcomes</p>
          </div>
          <div className="tile tile--miss">
            <p className="tile__num">{data.summary.miss}</p>
            <p className="tile__label">Missed</p>
            <p className="tile__note">Pulled, and left on the record</p>
          </div>
          <div className="tile tile--pending">
            <p className="tile__num">{data.summary.estimated}</p>
            <p className="tile__label">Still measuring</p>
            <p className="tile__note">Not counted either way yet</p>
          </div>
        </div>
      </div>

      {/* ---- What the agent reads ---- */}
      <div className="panel">
        <h2 className="panel__title">What your agent is reading</h2>
        <p className="panel__sub">
          {data.corpus.observations} things about your business, all of them still
          searchable when it looks for the next move.
        </p>
        <ul className="kinds">
          {data.kinds.map((k) => (
            <li key={k.kind} className="kind">
              <span className="kind__name">{KIND_LABEL[k.kind] ?? k.kind}</span>
              <span className="kind__track">
                <span
                  className="kind__fill"
                  style={{ width: `${(k.count / kindMax) * 100}%` }}
                />
              </span>
              <span className="kind__count">{k.count}</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
