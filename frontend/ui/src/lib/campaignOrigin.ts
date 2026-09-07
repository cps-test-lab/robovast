import type { CampaignOrigin } from '@/lib/robovastClient'

/**
 * What a campaign's origin hover lists.
 *
 * Here rather than in the component because these are decisions, not markup: which rows
 * there are, in what order, and what counts as a re-run. They are also the parts a
 * regression would break silently.
 */

/** A label→value pair for the hover. An empty value means the row is not shown. */
export type OriginFact = { label: string; value: string }

/** True for a re-run of an earlier campaign. */
export function isRerun(origin: CampaignOrigin): boolean {
  // `kind` is the single authority. Deliberately NOT derived from `from_campaign` being
  // set: it happens to imply a re-run today, and would be wrong the day a third kind exists.
  return origin.kind === 'retrigger'
}

/**
 * Which config version a re-run read, as one line -- or '' when nothing was recorded.
 *
 * A re-run migrates a staged copy of the parent's frozen `.vast`, so two runs of "the same
 * campaign" can read different config versions, and results that came out of different
 * versions are not results of the same experiment. The step list is what separates the two
 * answers worth having: empty means the config was read exactly as written, and only a null
 * `config_version_from` means nobody recorded it.
 */
export function configVersionFact(origin: CampaignOrigin): string {
  // The two fields are written together, so the version answers for both: a service that
  // predates them sends neither, and this says nothing rather than guessing "as written".
  if (!isRerun(origin) || origin.config_version_from == null) return ''
  const steps = origin.config_migration_steps
  if (steps.length === 0) return `v${origin.config_version_from}, as written`
  // The last step names the version the ladder reached, which is the version this run ran.
  const reached = steps[steps.length - 1].split('_to_').pop()
  return `v${origin.config_version_from} → v${reached}, migrated`
}

/** The rows of the hover panel, in reading order. Empty values are dropped downstream. */
export function originFacts(origin: CampaignOrigin): OriginFact[] {
  return [
    { label: 'Rerun of', value: isRerun(origin) ? origin.from_campaign : '' },
    { label: 'Config', value: configVersionFact(origin) },
    { label: 'Workspace', value: origin.workspace_name },
    { label: 'ID', value: origin.workspace_id },
    { label: 'File', value: origin.config_path },
  ]
}
