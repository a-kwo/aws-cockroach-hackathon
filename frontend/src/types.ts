/* Shapes exported by scripts/export_fixture.py, which reads the live cluster.
 *
 * These are the contract the API must honour. Building the UI against real
 * exported rows rather than invented mock data is what keeps the two from
 * drifting apart.
 */

export type Verdict = "verified" | "estimated" | "miss";

export type FindStatus =
  | "proposed"
  | "accepted"
  | "later"
  | "rejected"
  | "live"
  | "retired";

export interface Evidence {
  observation_id: string;
  rank: number;
  similarity: number;
  content: string;
  kind: string;
  source_name: string | null;
  subject: string | null;
  observed_at: string;
}

export interface Find {
  id: string;
  emoji: string | null;
  title: string;
  rationale: string;
  move: string | null;
  predicted_daily_cents: number;
  confidence: number;
  verify_after: string;
  status: FindStatus;
  created_at: string;

  /** Null until the Meter has judged it. */
  verdict: Verdict | null;
  actual_daily_cents: number | null;
  method: string | null;
  note: string | null;
  measured_at: string | null;
  period_start: string | null;
  period_end: string | null;

  evidence: Evidence[];
}

export interface Summary {
  verified: number;
  estimated: number;
  miss: number;
  judged: number;
  verified_daily_cents: number;
  /** Null before anything has been judged — 0% would read as total failure. */
  hit_rate: number | null;
}

export interface Business {
  id: string;
  name: string;
  category: string;
  city: string | null;
  region: string | null;
  goal_monthly_cents: number | null;
  goal_note: string | null;
}

export interface Corpus {
  observations: number;
  earliest: string;
  latest: string;
}

export interface AgentRun {
  id: string;
  agent: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  note: string | null;
}

/** One month of the ledger. Aggregated in SQL so the UI never derives money. */
export interface MonthlyLedger {
  month: string;
  verified_daily_cents: number;
  verified: number;
  miss: number;
}

export interface KindCount {
  kind: string;
  count: number;
}

export interface RatingWeek {
  week: string;
  avg_rating: number;
  reviews: number;
}

export interface DemoData {
  business: Business;
  summary: Summary;
  corpus: Corpus;
  finds: Find[];
  runs: AgentRun[];
  monthly: MonthlyLedger[];
  kinds: KindCount[];
  ratings: RatingWeek[];
}
