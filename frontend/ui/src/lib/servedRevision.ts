/**
 * Whether the service answering is running a different build than when this tab first asked.
 *
 * The version panel polls, so an upgrade started anywhere -- the CLI, another tab, this page
 * with its reload declined -- eventually swaps the numbers on screen. Swapping them silently
 * is the wrong ending: this tab is then holding a frontend build whose hashed chunks the
 * service no longer serves, so every view it has not opened yet is already broken, and the
 * only reading that says so is a revision the user would have to have memorised.
 *
 * The comparison is against the first revision *read*, not the one this tab booted from,
 * which is the honest reference available here: the service reports what it is running, and
 * nothing in a served document records it. That makes the answer per-tab and not per-mount,
 * hence a module-level reference rather than component state.
 *
 * `servedBuild` answers the neighbouring question -- are this tab's chunk URLs still served
 * -- from index.html, and is the one an error boundary needs. This one is cheaper and earlier:
 * it needs no extra request, because the panel has already made it.
 */

let firstSeen: string | null = null

/**
 * Record a revision the service reported, and answer whether the build changed under us.
 *
 * A missing revision is "cannot tell", never "changed": a source checkout reports none, and
 * a tab must not raise a restart warning over a question nobody could answer. The first
 * usable value becomes this tab's reference and is never itself a change.
 */
export function revisionChanged(revision: string | null | undefined): boolean {
  if (!revision) return false
  if (firstSeen === null) {
    firstSeen = revision
    return false
  }
  return revision !== firstSeen
}

/**
 * Adopt a revision as the new reference, so a dismissed warning stays dismissed.
 *
 * Without this the next poll would re-raise the same comparison against the same old
 * reference, and the warning could not be closed -- only outlasted.
 */
export function acceptRevision(revision: string | null | undefined): void {
  if (revision) firstSeen = revision
}

/** Forget the reference. For tests, which need a fresh tab per case. */
export function resetSeenRevision(): void {
  firstSeen = null
}
