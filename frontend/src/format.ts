/* Formatting helpers.
 *
 * Money arrives as integer cents and is only ever converted for display. No
 * arithmetic happens here — the backend owns the maths, and a float in the UI
 * that drifts from a stored cent value would make the ledger look wrong.
 */

/** "$23" or "$23.50" — trailing ".00" is noise on a dashboard. */
export function money(cents: number): string {
  const dollars = cents / 100;
  const hasCents = cents % 100 !== 0;
  return dollars.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: hasCents ? 2 : 0,
    maximumFractionDigits: hasCents ? 2 : 0,
  });
}

/** A daily rate expressed monthly, which is how owners think about money. */
export function perMonth(centsPerDay: number): string {
  return money(Math.round(centsPerDay * 30));
}

export function shortDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });
}

export function dayCount(iso: string, from: Date = new Date()): number {
  const then = new Date(iso);
  return Math.round((then.getTime() - from.getTime()) / 86_400_000);
}

/**
 * "in 12 days" / "3 days ago" / "today".
 *
 * Relative time is right here because the only question an owner asks about a
 * date on this screen is "how long until I know?"
 */
export function relativeDays(iso: string, from: Date = new Date()): string {
  const days = dayCount(iso, from);
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days === -1) return "yesterday";
  return days > 0 ? `in ${days} days` : `${Math.abs(days)} days ago`;
}

/**
 * Confidence as plain language.
 *
 * A restaurant owner has no use for "0.82". What they need to know is how much
 * weight to put on the number next to it.
 */
export function confidenceWords(confidence: number): string {
  if (confidence >= 0.85) return "We're confident";
  if (confidence >= 0.7) return "We're fairly confident";
  if (confidence >= 0.55) return "Worth a try";
  return "A long shot";
}

/** Retrieval similarity as a percentage, for the evidence trail. */
export function similarityPercent(similarity: number): string {
  return `${Math.round(similarity * 100)}%`;
}
