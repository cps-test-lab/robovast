import { describe, expect, it } from 'vitest'
import type { ServiceSetting } from '@/lib/robovastClient'
import { groupsOf, OTHER, WITHHELD_REASON } from './serviceConfig'

function setting(key: string, group: string, extra: Partial<ServiceSetting> = {}) {
  return { key, group, description: '', is_set: true, value: null,
           default: null, withheld: null, ...extra } as ServiceSetting
}

describe('groupsOf', () => {
  it('sorts named groups and keeps each group\'s settings together', () => {
    const groups = groupsOf([
      setting('B_ONE', 'Result share'),
      setting('A_ONE', 'Access'),
      setting('B_TWO', 'Result share'),
    ])

    expect(groups.map(([name]) => name)).toEqual(['Access', 'Result share'])
    expect(groups[1][1].map((s) => s.key)).toEqual(['B_ONE', 'B_TWO'])
  })

  it('puts unrecognised settings last rather than dropping them', () => {
    // The service reports a key it does not recognise with an empty group. It IS in force,
    // so hiding it would misreport the service — but it has no description, so it does not
    // belong at the top either.
    const groups = groupsOf([
      setting('ROBOVAST_MYSTERY', '', { withheld: 'unclassified' }),
      setting('ROBOVAST_NTFY_TOPIC', 'Notifications'),
    ])

    expect(groups.map(([name]) => name)).toEqual(['Notifications', OTHER])
    expect(groups[1][1][0].key).toBe('ROBOVAST_MYSTERY')
  })

  it('omits the Other heading when every setting is recognised', () => {
    const groups = groupsOf([setting('ROBOVAST_NTFY_TOPIC', 'Notifications')])

    expect(groups.map(([name]) => name)).toEqual(['Notifications'])
  })
})

describe('WITHHELD_REASON', () => {
  it('explains every reason the service can send', () => {
    // A reason with no entry renders an empty tooltip — a row saying "set" with nothing
    // saying why the value is missing, which is the one state the panel must not reach.
    for (const reason of ['secret', 'server_only', 'host_path', 'unclassified']) {
      expect(WITHHELD_REASON[reason]).toBeTruthy()
    }
  })
})
