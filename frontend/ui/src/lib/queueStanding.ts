// A live campaign's standing with the cluster queue, as a card shows it and the priority prompt
// accepts it. Markup stays in `pages/`; this is the part a test can pin.

/** The chip label for a campaign's rank, or null at the default.
 *
 * Null at 0 because a label reading "prio 0" on every campaign costs every row a glance and says
 * nothing. A positive rank carries its sign so the chip reads as an offset from normal. */
export function priorityLabel(priority: number): string | null {
  if (priority === 0) return null
  return `prio ${priority > 0 ? `+${priority}` : priority}`
}

/** Why typed text is not a priority, or null when it is one.
 *
 * Refused rather than coerced: `Number('')` is 0, which would silently reset the campaign to
 * normal when somebody cleared the field meaning to cancel, and `Number('1.5')` is a rank the
 * queue does not have. */
export function priorityInputError(typed: string): string | null {
  const t = typed.trim()
  return t && Number.isInteger(Number(t)) ? null : 'Priority must be a whole number.'
}
