// Which address the Config view reads a project from. Tested because the campaign form is the one
// place the read-only guarantee is *constructed* rather than checked: a campaign source must resolve
// into `/results/...`, which has no write route, and must never produce a `/sources/...` address.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import {
  configDirUrl,
  configFileUrl,
  configFilesKey,
  isEmptySource,
  isReadOnlySource,
  type ConfigSource,
} from './configSource'

const WS: ConfigSource = { kind: 'workspace', id: 'ws-abc123' }
const CAMPAIGN: ConfigSource = { kind: 'campaign', id: 'nav-2026-08-12' }

describe('addresses', () => {
  it('reads a workspace from the writable sources namespace', () => {
    expect(configDirUrl(WS)).toBe('/sources/ws-abc123/')
    expect(configFileUrl(WS, 'pilot.vast')).toBe('/sources/ws-abc123/pilot.vast')
  })

  it('reads a campaign from its _config/ in the read-only results namespace', () => {
    expect(configDirUrl(CAMPAIGN)).toBe('/results/nav-2026-08-12/_config/')
    expect(configFileUrl(CAMPAIGN, 'pilot.vast'))
      .toBe('/results/nav-2026-08-12/_config/pilot.vast')
  })

  it('never builds a sources address for a campaign', () => {
    expect(configDirUrl(CAMPAIGN)).not.toContain('/sources/')
    expect(configFileUrl(CAMPAIGN, 'a/b.osc')).not.toContain('/sources/')
  })

  it('encodes segments individually so the path separators survive', () => {
    expect(configFileUrl(CAMPAIGN, 'scenarios/my scenario.osc'))
      .toBe('/results/nav-2026-08-12/_config/scenarios/my%20scenario.osc')
  })
})

describe('flags', () => {
  it('marks only a campaign read-only', () => {
    expect(isReadOnlySource(CAMPAIGN)).toBe(true)
    expect(isReadOnlySource(WS)).toBe(false)
  })

  it('treats an unselected workspace as empty', () => {
    expect(isEmptySource({ kind: 'workspace', id: '' })).toBe(true)
    expect(isEmptySource(WS)).toBe(false)
  })
})

describe('configFilesKey', () => {
  it('keeps a workspace on the bare key its writers invalidate', () => {
    // useCreateVast / useDirectoryUpload invalidate ['files', workspaceId], and LaunchBar reads
    // it — a key of its own here would leave the Config view's list stale after a create.
    expect(configFilesKey(WS)).toEqual(['files', 'ws-abc123'])
  })

  it('gives a campaign a key nothing else touches', () => {
    expect(configFilesKey(CAMPAIGN)).toEqual(['files', 'campaign:nav-2026-08-12'])
  })
})
