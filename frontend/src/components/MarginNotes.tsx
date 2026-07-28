/* The right margin — the signature element, finally in the margin.
 *
 * Retrieval printed as footnotes beside the argument, the way a serious document
 * cites itself. This is why the reading column is 680px and not stretched: the
 * space either side is not waste, it is apparatus.
 *
 * Standing open rather than behind a click. A drawer makes proving the claim an
 * action Rosa has to decide to take; a margin makes it something she cannot help
 * seeing. For a product whose whole promise is "we show the receipts", the
 * receipts should not be one tap away — they should be adjacent.
 *
 * Full ink on every note. Strength is rank order and plain words, never contrast
 * and never a threshold — two verified wins in this very dataset have weaker
 * evidence than the published miss.
 */

import type { Find } from "../types";
import { shortDate } from "../format";

const SOURCE: Record<string, string> = {
  review: "customer",
  social: "local forum",
  trend: "trend",
  rival_price: "rival price",
  rival_menu: "rival menu",
  owner_upload: "you",
};

export function MarginNotes({
  find,
  onExpand,
}: {
  find: Find | undefined;
  onExpand: (find: Find) => void;
}) {
  if (!find || find.evidence.length === 0) {
    return (
      <aside className="margin" aria-hidden="true">
        <p className="margin__quiet">Nothing filed this morning.</p>
      </aside>
    );
  }

  const shown = find.evidence.slice(0, 5);
  const rest = find.evidence.length - shown.length;

  return (
    <aside className="margin" aria-label="What this was built on">
      <h2 className="margin__head head">What this was built on</h2>

      <ol className="margin__notes">
        {shown.map((item, i) => (
          <li key={item.observation_id} className="mnote">
            <span className="mnote__num fig" aria-hidden="true">
              {i + 1}
            </span>
            <p className="mnote__quote">{item.content}</p>
            <p className="mnote__src head">
              {SOURCE[item.kind] ?? item.kind}
              {" · "}
              <span className="fig">{shortDate(item.observed_at)}</span>
            </p>
          </li>
        ))}
      </ol>

      <button className="margin__more" onClick={() => onExpand(find)}>
        {rest > 0 ? (
          <>
            <span className="fig">{rest}</span> more, and the full text →
          </>
        ) : (
          <>Read all {find.evidence.length} in full →</>
        )}
      </button>
    </aside>
  );
}
