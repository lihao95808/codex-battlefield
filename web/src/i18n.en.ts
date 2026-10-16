/**
 * English dictionary (P2-6).
 *
 * Typed as `Record<DictKey, string>` against the Chinese dictionary, so a missing or
 * extra key fails the **build**, not merely a test. `i18n.test.ts` checks the same
 * property at runtime — the two are not redundant: the type covers hand-written entries,
 * the test also covers whatever the object actually holds at runtime.
 *
 * Translation policy (a real choice, so it is written down):
 *
 * * **Coined terms are kept, not softened.** "Obligation" here is a ledger entry that
 *   blocks a parent from being marked done — not "a thing one ought to do". Rendering it
 *   as "todo" would make the core mechanism unreadable. The glossary explains each term's
 *   local meaning instead of laundering it into a familiar word.
 * * **State messages keep the cost, not just the status.** "Unbound" alone is useless;
 *   the English has to say what falls back to where.
 */
import type { DictKey } from './i18n.zh';

export const EN: Record<DictKey, string> = {
  // ---------- language switch ----------
  'lang.label': 'Language',
  // Both languages name each option in its own script, so someone who cannot read the
  // current language can still find the way out.
  'lang.switchTo.zh': '\u5207\u6362\u5230\u4e2d\u6587',
  'lang.switchTo.en': 'Switch to English',

  // ---------- glossary ----------
  'glossary.title': 'Glossary',
  'glossary.lead':
    'Most terms here are coined by this tool, or **redefined** by it. Reading them in their everyday sense will misframe every judgement that follows.',
  'glossary.col.interop': 'How it differs from the everyday word (the point of this table)',
  'glossary.note':
    'Full definitions live in the SPEC documents under docs/. This table only explains how each term differs from its everyday meaning; it does not replace the SPEC.',
};
