import { describe, expect, it } from 'vitest'
import type { CampaignDeletion, CampaignSummary } from '@/lib/robovastClient'
import {
  deletionSummary,
  isSelectable,
  pruneSelection,
  selectedInListOrder,
} from './campaignSelection'

const camp = (campaign_id: string, phase: string) =>
  ({ campaign_id, phase }) as unknown as CampaignSummary

const done = (campaign_id: string, outcome: CampaignDeletion['outcome'], ok: boolean) =>
  ({ campaign_id, outcome, ok, message: '' }) as CampaignDeletion

describe('isSelectable', () => {
  it('offers a finished campaign and not a running one', () => {
    // The service refuses a running campaign; a checkbox on it could only produce a refusal.
    expect(isSelectable(camp('a', 'completed'))).toBe(true)
    expect(isSelectable(camp('b', 'running'))).toBe(false)
  })
})

describe('pruneSelection', () => {
  it('drops a campaign that disappeared or became busy again', () => {
    const selected = new Set(['gone', 'busy', 'kept'])
    const list = [camp('busy', 'postprocessing'), camp('kept', 'completed')]
    expect([...pruneSelection(selected, list)]).toEqual(['kept'])
  })

  it('returns the same set when nothing changed, so no state update follows', () => {
    const selected = new Set(['a'])
    expect(pruneSelection(selected, [camp('a', 'completed')])).toBe(selected)
  })
})

describe('selectedInListOrder', () => {
  it('sends the picked ids in the order the list shows them', () => {
    const list = [camp('c', 'completed'), camp('a', 'completed'), camp('b', 'completed')]
    expect(selectedInListOrder(new Set(['a', 'c']), list)).toEqual(['c', 'a'])
  })
})

describe('deletionSummary', () => {
  it('counts an id that was already gone with the deleted ones', () => {
    // `ok` is the service's verdict that nothing of it is left, which is true of both.
    const s = deletionSummary([done('a', 'deleted', true), done('b', 'not_found', true)])
    expect(s).toEqual({ ok: 2, failed: 0, text: '2 campaigns deleted' })
  })

  it('says how many were not deleted when any failed', () => {
    const s = deletionSummary([
      done('a', 'deleted', true), done('b', 'running', false), done('c', 'partial', false),
    ])
    expect(s.failed).toBe(2)
    expect(s.text).toBe('1 of 3 deleted, 2 not')
  })
})
