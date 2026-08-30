import type { ServiceSetting } from '@/lib/robovastClient'

// Why a value is not on screen, in the reader's terms. `withheld` is the resolved outcome
// for THIS caller, not the setting's classification -- a host path is withheld from a
// remote browser and shown to one on the service's own machine -- so these are phrased as
// what happened to this request rather than as a property of the setting.
export const WITHHELD_REASON: Record<string, string> = {
  secret: 'A credential. Its value never leaves the service, in any form.',
  server_only:
    'A registry detail. Those stay server-side: they never cross the client interface.',
  host_path:
    "A path on the service's own machine, shown only to a caller on that machine.",
  unclassified:
    'RoboVAST does not recognise this setting, so its value is withheld in case it is a '
    + 'credential. It is in force all the same.',
}

/** Heading for settings the service reported without a group — see `groupsOf`. */
export const OTHER = 'Other'

/**
 * Settings by group: named groups alphabetically, unrecognised keys last.
 *
 * The service reports a key it does not recognise with an empty `group`, and those go
 * under `OTHER` at the end. They are real settings and must be visible — the panel would
 * misreport the service if it dropped them — but they are the ones with no description, so
 * leading with them would put the least readable rows first.
 */
export function groupsOf(settings: ServiceSetting[]): [string, ServiceSetting[]][] {
  const by = new Map<string, ServiceSetting[]>()
  for (const setting of settings) {
    const key = setting.group || OTHER
    const rows = by.get(key)
    if (rows) rows.push(setting)
    else by.set(key, [setting])
  }
  const named = [...by.keys()].filter((g) => g !== OTHER).sort()
  return [...named, ...(by.has(OTHER) ? [OTHER] : [])]
    .map((group) => [group, by.get(group)!] as [string, ServiceSetting[]])
}
