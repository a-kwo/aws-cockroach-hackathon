/* Turning agent prose into something readable in ninety seconds.
 *
 * The spec measured every `move` field in the real export and found FOUR shapes,
 * not one:
 *
 *   enumerated (1)(2)(3)        4 fields, 813–1097 chars
 *   enumerated (a)(b)(c)        1 field,  1054 chars
 *   prose, semicolons, no marks 2 fields, 808 and 884 chars
 *   a single sentence          10 fields, 53–126 chars
 *
 * A parser that assumes the first shape silently destroys six of the seventeen
 * long-form fields. So this splits on whichever enumerator is actually present,
 * falls back to sentence boundaries, and returns the whole thing untouched when
 * it is already short.
 *
 * Nothing here summarises and nothing is discarded. The full string is always
 * available; clamping is CSS, so find-in-page, screen readers and copy-paste
 * all still get every word.
 */

export interface Step {
  /** "1", "a", or "" when the source had no enumerator. */
  marker: string;
  text: string;
  /**
   * Whose job this is. Detected from voice: first-person agent verbs ("I will
   * draft") versus second-person approval verbs ("you review", "your sign-off").
   * This is what lets the header price her workload before she reads a step.
   */
  owner: "agent" | "owner";
}

export interface ParsedMove {
  /** The clause before the first enumerator, promoted to the lede. */
  lede: string;
  steps: Step[];
  /** Sentences that name a limit or a promise — the guardrails. */
  guards: string[];
  wordCount: number;
  /** True when the source was a single sentence and needs no decomposition. */
  isShort: boolean;
}

const ENUM_PATTERNS = [
  /\((\d+)\)\s*/g,
  /\(([a-z])\)\s*/g,
  /(?:^|\s)(\d+)[.)]\s+/g,
];

/** Sentences that answer "what does this cost me and what can't you do?" */
const GUARD_HINTS = [
  /\bnot?\s+exceed\b/i,
  /\bwithout your\b/i,
  /\bbefore (?:anything|it|any)\b/i,
  /\bnothing (?:gets|runs|sends|goes)\b/i,
  /\byour (?:approval|sign-?off|OK|yes)\b/i,
  /\bwill not\b/i,
  /\bno (?:menu )?prices?\b/i,
  /\bcap\b/i,
];

const OWNER_VOICE = [
  /\byou (?:review|approve|send|OK|okay|sign|decide|choose|confirm)\b/i,
  /\byour (?:sign-?off|approval|call|decision)\b/i,
  /\bif you\b/i,
];

const AGENT_VOICE = [/\bI(?:'ll| will| am|'ve)?\b/i, /\bwe(?:'ll| will)\b/i];

function classify(text: string): "agent" | "owner" {
  // Owner voice wins ties: over-reporting her workload is the safer error.
  if (OWNER_VOICE.some((r) => r.test(text))) return "owner";
  if (AGENT_VOICE.some((r) => r.test(text))) return "agent";
  return "agent";
}

function splitSentences(text: string): string[] {
  return text
    .split(/(?<=[.!?])\s+(?=[A-Z(])/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function findEnumerator(text: string): { marker: string; index: number }[] {
  for (const pattern of ENUM_PATTERNS) {
    pattern.lastIndex = 0;
    const hits: { marker: string; index: number }[] = [];
    let match: RegExpExecArray | null;
    while ((match = pattern.exec(text)) !== null) {
      hits.push({ marker: match[1], index: match.index });
    }
    // Two enumerators is the minimum that proves a list rather than a stray
    // parenthetical like "(e.g. 35-50 minutes)".
    if (hits.length >= 2) return hits;
  }
  return [];
}

export function parseMove(move: string | null): ParsedMove {
  const text = (move ?? "").trim();
  const wordCount = text ? text.split(/\s+/).length : 0;

  if (!text) {
    return { lede: "", steps: [], guards: [], wordCount: 0, isShort: true };
  }

  // Ten of seventeen fields are one sentence. Do nothing to them.
  if (wordCount <= 30) {
    return { lede: text, steps: [], guards: [], wordCount, isShort: true };
  }

  const enumerators = findEnumerator(text);
  let lede = "";
  let steps: Step[] = [];

  if (enumerators.length) {
    lede = text.slice(0, enumerators[0].index).trim().replace(/[:,;]\s*$/, "");
    steps = enumerators.map((hit, i) => {
      const end =
        i + 1 < enumerators.length ? enumerators[i + 1].index : text.length;
      const body = text
        .slice(hit.index, end)
        .replace(/^\(?[\da-z]+\)?[.)]?\s*/i, "")
        .trim()
        .replace(/[;,]\s*$/, "");
      return { marker: hit.marker, text: body, owner: classify(body) };
    });
  } else {
    // Semicolon-structured prose with no enumerators: the semicolons ARE the
    // list, so use them before falling back to sentences.
    const parts =
      text.split(";").length >= 3 ? text.split(";") : splitSentences(text);
    const cleaned = parts.map((p) => p.trim()).filter(Boolean);
    lede = cleaned[0] ?? text;
    steps = cleaned.slice(1).map((body) => ({
      marker: "",
      text: body.replace(/[;,]\s*$/, ""),
      owner: classify(body),
    }));
  }

  // Guardrails are pulled out of wherever they were buried — in the production
  // text they sit on line nine — and shown next to the decision instead.
  const guards = splitSentences(text).filter((s) =>
    GUARD_HINTS.some((r) => r.test(s)),
  );

  return { lede: lede || text, steps, guards, wordCount, isShort: false };
}

/**
 * "Four things I'll draft. One is yours." — her workload, before she reads a
 * step. The judges called this the actual ninety-second payload: the question
 * she is asking is not "what is the plan" but "what does this cost me".
 */
export function workloadHeadline(steps: Step[]): string {
  if (!steps.length) return "";
  const mine = steps.filter((s) => s.owner === "owner").length;
  const theirs = steps.length - mine;
  const thing = theirs === 1 ? "thing" : "things";

  if (mine === 0) return `${theirs} ${thing} I'll draft. None of them are yours to do.`;
  if (mine === 1) return `${theirs} ${thing} I'll draft. One is yours.`;
  return `${theirs} ${thing} I'll draft. ${mine} are yours.`;
}
