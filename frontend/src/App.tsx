import { useMemo, useState } from "react";

import demo from "./fixtures/demo.json";
import type { DemoData, Find, FindStatus } from "./types";
import { FindSheet } from "./components/FindSheet";
import { RecordRail } from "./components/RecordRail";
import { ProofMargin } from "./components/ProofMargin";
import { Road } from "./components/Road";
import { MarginNotes } from "./components/MarginNotes";
import "./styles/tokens.css";
import "./styles/desk.css";

const data = demo as unknown as DemoData;

export default function App() {
  const [decisions, setDecisions] = useState<Record<string, FindStatus>>({});
  const [proofFor, setProofFor] = useState<Find | null>(null);

  const finds = useMemo(
    () =>
      data.finds.map((f) => (decisions[f.id] ? { ...f, status: decisions[f.id] } : f)),
    [decisions],
  );

  const undecided = finds.filter((f) => f.verdict === null && f.status === "proposed");
  const open = finds.filter(
    (f) => f.verdict === null && (f.status === "proposed" || f.status === "later"),
  );
  const tonight = undecided[0];
  const decidedCount = Object.keys(decisions).length;

  const handleDecide = (findId: string, status: FindStatus) =>
    setDecisions((prev) => ({ ...prev, [findId]: status }));

  return (
    <div className="desk">
      <header className="masthead">
        <div className="masthead__brand">
          <svg className="masthead__mark" viewBox="0 0 22 22" aria-hidden="true">
            <rect x="1" y="1" width="20" height="20" rx="2" fill="none" stroke="currentColor" strokeWidth="1.4" />
            <line x1="5.5" y1="7" x2="16.5" y2="7" stroke="currentColor" strokeWidth="1.4" />
            <line x1="5.5" y1="11" x2="16.5" y2="11" stroke="currentColor" strokeWidth="1.4" />
            <circle cx="11" cy="16" r="1.9" fill="currentColor" />
          </svg>
          <span className="masthead__name">Brass Tacks</span>
        </div>
        <p className="masthead__slug head">
          {data.business.name}
          {data.business.city && <> · {data.business.city}</>}
        </p>
      </header>

      <div className="chassis">
        <RecordRail
          finds={finds}
          summary={data.summary}
          corpusCount={data.corpus.observations}
          onShowProof={setProofFor}
        />

        <main className="column">
          {tonight ? (
            <FindSheet
              find={tonight}
              summary={data.summary}
              issueNumber={finds.length - undecided.length + 1}
              onDecide={handleDecide}
              onShowProof={setProofFor}
            />
          ) : (
            <article className="sheet sheet--quiet">
              <p className="sheet__kicker head">This morning</p>
              <h1 className="sheet__title">Nothing needs you.</h1>
              <p className="sheet__lede">
                {decidedCount > 0
                  ? `You settled ${decidedCount} this morning. The agents run again tonight and will file whatever they find.`
                  : "The agents ran overnight and found nothing worth interrupting you for. Go run your shop."}
              </p>
              <p className="sheet__byline head">
                Radar, Analyst and Meter ran overnight · nothing filed
              </p>
            </article>
          )}

          <Road
            realizedDailyCents={data.summary.verified_daily_cents}
            openFinds={open}
            goalMonthlyCents={data.business.goal_monthly_cents}
            monthsOfLedger={data.monthly.length}
            onShowProof={setProofFor}
          />
        </main>

        <MarginNotes find={tonight} onExpand={setProofFor} />
      </div>

      <ProofMargin find={proofFor} onClose={() => setProofFor(null)} />
    </div>
  );
}
