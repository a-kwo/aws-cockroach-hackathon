import { useMemo, useState } from "react";

import demo from "./fixtures/demo.json";
import type { DemoData, Find, FindStatus } from "./types";
import { MorningDecision } from "./components/MorningDecision";
import { ReceiptLedger } from "./components/ReceiptLedger";
import { EvidenceTrail } from "./components/EvidenceTrail";
import "./styles/tokens.css";
import "./styles/app.css";

const data = demo as unknown as DemoData;

/* Statuses that still want a decision from the owner. 'later' stays in the
 * queue deliberately — the Later jar is a deferral, not a dismissal. */
const AWAITING: FindStatus[] = ["proposed"];

export default function App() {
  const [decisions, setDecisions] = useState<Record<string, FindStatus>>({});
  const [evidenceFor, setEvidenceFor] = useState<Find | null>(null);

  const finds = useMemo(
    () =>
      data.finds.map((find) =>
        decisions[find.id] ? { ...find, status: decisions[find.id] } : find,
      ),
    [decisions],
  );

  const queue = finds.filter(
    (f) => f.verdict === null && AWAITING.includes(f.status),
  );
  const decidedToday = Object.keys(decisions).length;

  const handleDecide = (findId: string, status: FindStatus) =>
    setDecisions((prev) => ({ ...prev, [findId]: status }));

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar__brand">
          <span className="topbar__mark" aria-hidden="true" />
          <span className="topbar__name">Brass Tacks</span>
        </div>
        <div className="topbar__shop">
          {data.business.name}
          {data.business.city && <span> · {data.business.city}</span>}
        </div>
      </header>

      <main className="main">
        <MorningDecision
          find={queue[0]}
          summary={data.summary}
          queued={queue.length}
          onDecide={handleDecide}
          onShowEvidence={setEvidenceFor}
        />

        {decidedToday > 0 && queue.length === 0 && (
          <p className="app__wrapup">
            That&rsquo;s everything. {decidedToday} decided this morning.
          </p>
        )}

        <ReceiptLedger
          finds={finds}
          summary={data.summary}
          onShowEvidence={setEvidenceFor}
        />

        <footer className="footnote">
          <p>
            Your agent has read <strong>{data.corpus.observations}</strong>{" "}
            things about your business and remembers all of them.
          </p>
          <p className="footnote__quiet">
            Seeded demonstration data for a fictional restaurant.
          </p>
        </footer>
      </main>

      <EvidenceTrail find={evidenceFor} onClose={() => setEvidenceFor(null)} />
    </div>
  );
}
