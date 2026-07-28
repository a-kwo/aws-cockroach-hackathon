/* The road to the goal.
 *
 * The single most useful chart in the product: what is already earning, what
 * each undecided find would add, and how far that leaves you from the goal you
 * set. Every segment is a decision the owner can actually make today.
 *
 * Form: a stacked progress bar rather than a line or pie. The job is
 * "composition against a target", and the owner's real question is "what gets me
 * there", which a stack answers by construction — each segment is one tap.
 *
 * Money is never recomputed here; cents come from the ledger and are only
 * converted for display.
 */

import { useState } from "react";
import type { Find } from "../types";
import { money } from "../format";

interface Props {
  realizedDailyCents: number;
  openFinds: Find[];
  goalMonthlyCents: number | null;
  goalNote: string | null;
  onShowEvidence: (find: Find) => void;
}

const PER_MONTH = 30;

export function PathToGoal({
  realizedDailyCents,
  openFinds,
  goalMonthlyCents,
  goalNote,
  onShowEvidence,
}: Props) {
  const [hovered, setHovered] = useState<string | null>(null);

  if (!goalMonthlyCents) return null;

  const realizedMonthly = realizedDailyCents * PER_MONTH;
  const openMonthly = openFinds.map((f) => ({
    find: f,
    monthly: f.predicted_daily_cents * PER_MONTH,
  }));
  const openTotal = openMonthly.reduce((sum, o) => sum + o.monthly, 0);
  const projected = realizedMonthly + openTotal;
  const shortfall = Math.max(0, goalMonthlyCents - projected);

  // Scale to whichever is larger so an over-target projection is still readable.
  const scale = Math.max(goalMonthlyCents, projected);
  const pct = (cents: number) => (cents / scale) * 100;

  return (
    <section className="goal">
      <header className="goal__head">
        <h2 className="goal__title">The road to {money(goalMonthlyCents)} a month</h2>
        <p className="goal__sub">
          What you&rsquo;re earning now, plus everything waiting on a decision
          {goalNote && ` — ${goalNote}`}.
        </p>
      </header>

      <div className="goal__figure">
        <div
          className="goal__track"
          role="img"
          aria-label={`${money(realizedMonthly)} earning now, ${money(openTotal)} awaiting a decision, against a goal of ${money(goalMonthlyCents)} a month`}
        >
          <span
            className="goal__seg goal__seg--realized"
            style={{ width: `${pct(realizedMonthly)}%` }}
            onMouseEnter={() => setHovered("realized")}
            onMouseLeave={() => setHovered(null)}
          />
          {openMonthly.map(({ find, monthly }) => (
            <span
              key={find.id}
              className={`goal__seg goal__seg--open${
                hovered === find.id ? " is-hovered" : ""
              }`}
              style={{ width: `${pct(monthly)}%` }}
              onMouseEnter={() => setHovered(find.id)}
              onMouseLeave={() => setHovered(null)}
            />
          ))}
          {shortfall > 0 && (
            <span
              className="goal__seg goal__seg--gap"
              style={{ width: `${pct(shortfall)}%` }}
              onMouseEnter={() => setHovered("gap")}
              onMouseLeave={() => setHovered(null)}
            />
          )}
        </div>

        <div className="goal__readout" aria-live="polite">
          {hovered === "realized" && (
            <p>
              <strong>{money(realizedMonthly)} a month</strong> — already earning,
              measured against real outcomes.
            </p>
          )}
          {hovered === "gap" && (
            <p>
              <strong>{money(shortfall)} a month</strong> still to find. Your agent
              keeps looking every night.
            </p>
          )}
          {hovered &&
            hovered !== "realized" &&
            hovered !== "gap" &&
            (() => {
              const match = openMonthly.find((o) => o.find.id === hovered);
              if (!match) return null;
              return (
                <p>
                  <strong>{money(match.monthly)} a month</strong> —{" "}
                  {match.find.title}. Waiting on you.
                </p>
              );
            })()}
          {!hovered && (
            <p className="goal__hint">
              Hover a segment. Every gold block is one decision away.
            </p>
          )}
        </div>
      </div>

      <ul className="goal__legend">
        <li>
          <span className="swatch swatch--realized" aria-hidden="true" />
          Earning now · {money(realizedMonthly)}
        </li>
        <li>
          <span className="swatch swatch--open" aria-hidden="true" />
          Waiting on you · {money(openTotal)}
        </li>
        {shortfall > 0 && (
          <li>
            <span className="swatch swatch--gap" aria-hidden="true" />
            Still to find · {money(shortfall)}
          </li>
        )}
      </ul>

      <ol className="stops">
        {openMonthly.map(({ find, monthly }, index) => (
          <li key={find.id} className="stop">
            <span className="stop__index" aria-hidden="true">
              {index + 1}
            </span>
            <div className="stop__body">
              <p className="stop__title">
                <span aria-hidden="true">{find.emoji ?? "💡"}</span> {find.title}
              </p>
              <p className="stop__meta">
                {money(monthly)} a month ·{" "}
                <button
                  className="stop__evidence"
                  onClick={() => onShowEvidence(find)}
                >
                  {find.evidence.length} sources
                </button>
              </p>
            </div>
            <span className={`stop__status stop__status--${find.status}`}>
              {find.status === "later" ? "Saved" : "Undecided"}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
