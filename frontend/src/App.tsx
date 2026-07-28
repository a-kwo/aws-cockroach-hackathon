import { useMemo, useState } from "react";

import demo from "./fixtures/demo.json";
import type { DemoData, Find, FindStatus } from "./types";
import { MorningDecision } from "./components/MorningDecision";
import { ReceiptLedger } from "./components/ReceiptLedger";
import { EvidenceTrail } from "./components/EvidenceTrail";
import { PathToGoal } from "./components/PathToGoal";
import { Numbers } from "./components/Numbers";
import { money } from "./format";
import "./styles/tokens.css";
import "./styles/app.css";
import "./styles/dashboard.css";

const data = demo as unknown as DemoData;

/* The tabs mirror the loop the agents actually run: observe what's out there,
 * decide what to do, check whether it worked. Naming them after the owner's
 * question rather than the agent's name keeps the interface on her side. */
const TABS = [
  { id: "morning", label: "This morning", hint: "What needs you" },
  { id: "road", label: "The road", hint: "What gets you to your goal" },
  { id: "numbers", label: "The numbers", hint: "Whether it's working" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function App() {
  const [tab, setTab] = useState<TabId>("morning");
  const [decisions, setDecisions] = useState<Record<string, FindStatus>>({});
  const [evidenceFor, setEvidenceFor] = useState<Find | null>(null);

  const finds = useMemo(
    () =>
      data.finds.map((find) =>
        decisions[find.id] ? { ...find, status: decisions[find.id] } : find,
      ),
    [decisions],
  );

  const undecided = finds.filter(
    (f) => f.verdict === null && f.status === "proposed",
  );
  const open = finds.filter(
    (f) => f.verdict === null && (f.status === "proposed" || f.status === "later"),
  );

  const handleDecide = (findId: string, status: FindStatus) =>
    setDecisions((prev) => ({ ...prev, [findId]: status }));

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar__brand">
          <span className="topbar__mark" aria-hidden="true" />
          <span className="topbar__name">Brass Tacks</span>
        </div>
        <div className="topbar__meta">
          <span className="topbar__earning">
            {money(data.summary.verified_daily_cents)}
            <span> a day, earning now</span>
          </span>
          <span className="topbar__shop">{data.business.name}</span>
        </div>
      </header>

      <nav className="tabs" aria-label="Sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`tab${tab === t.id ? " is-active" : ""}`}
            onClick={() => setTab(t.id)}
            aria-current={tab === t.id ? "page" : undefined}
          >
            <span className="tab__label">
              {t.label}
              {t.id === "morning" && undecided.length > 0 && (
                <span className="tab__badge">{undecided.length}</span>
              )}
            </span>
            <span className="tab__hint">{t.hint}</span>
          </button>
        ))}
      </nav>

      <main className="main">
        {tab === "morning" && (
          <>
            <MorningDecision
              find={undecided[0]}
              summary={data.summary}
              queued={undecided.length}
              onDecide={handleDecide}
              onShowEvidence={setEvidenceFor}
            />
            <ReceiptLedger
              finds={finds}
              summary={data.summary}
              onShowEvidence={setEvidenceFor}
            />
          </>
        )}

        {tab === "road" && (
          <PathToGoal
            realizedDailyCents={data.summary.verified_daily_cents}
            openFinds={open}
            goalMonthlyCents={data.business.goal_monthly_cents}
            goalNote={data.business.goal_note}
            onShowEvidence={setEvidenceFor}
          />
        )}

        {tab === "numbers" && <Numbers data={data} />}

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
