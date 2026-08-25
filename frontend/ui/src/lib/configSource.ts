import { resultsUrl, sourcesUrl } from './robovastClient'

// Which project the Config view is editing. Two kinds, because the service has two: a workspace
// holds authored inputs and is writable (`/sources/<ws>/`), while a campaign's frozen `_config/`
// lives in the read-only results tree (`/results/<id>/_config/`) and records what that campaign
// actually ran. The Config view reads either through this one descriptor rather than holding a
// workspace id and a campaign id and branching per call.
//
// A campaign source is read-only, and that is enforced twice over: nothing here can build a
// `/sources` address for a campaign, and `/results` has no write route at all (a PUT there is a 405
// from the router, not a permission check someone has to remember).

export interface ConfigSource {
  kind: 'workspace' | 'campaign'
  id: string
}

/** Stable identity for effects that must fire on a *change* of project. */
export const configSourceKey = (s: ConfigSource) => `${s.kind}:${s.id}`

/** The react-query key of this project's file listing.
 *
 *  A workspace keeps the bare `['files', <workspace_id>]` it always had, so the launcher's list and
 *  the two writers that invalidate it (useCreateVast, useDirectoryUpload) still share one cache
 *  entry with the Config view. Only a campaign needs a key of its own. */
export const configFilesKey = (s: ConfigSource) => [
  'files',
  s.kind === 'workspace' ? s.id : configSourceKey(s),
]

/** The project root, as an address — listable. */
export const configDirUrl = (s: ConfigSource) =>
  s.kind === 'campaign' ? resultsUrl(s.id, '_config/') : sourcesUrl(s.id, '')

/** One file inside the project, by its project-relative path. */
export const configFileUrl = (s: ConfigSource, path: string) =>
  s.kind === 'campaign' ? resultsUrl(s.id, `_config/${path}`) : sourcesUrl(s.id, path)

export const isReadOnlySource = (s: ConfigSource) => s.kind === 'campaign'

/** True when the source names nothing yet (no workspace selected) — nothing to load. */
export const isEmptySource = (s: ConfigSource) => !s.id
