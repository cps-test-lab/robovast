import { describe, expect, it } from 'vitest'

import { frameIndexUrl, frameUrl, liveFramesUrl, screenshotUrl, type RunRef } from './frameRoutes'

const run: RunRef = { campaignId: 'c 1', configName: 'cfg/a', runId: 3 }

describe('frameUrl', () => {
  it('names the run as <config>/<run> and the topic, with the moment when given', () => {
    expect(frameUrl('', run, '/cam/image_raw', 12.5)).toBe(
      '/data/campaigns/c%201/frame?run=cfg%2Fa%2F3&topic=%2Fcam%2Fimage_raw&t=12.5',
    )
  })

  it('omits t for the newest frame', () => {
    expect(frameUrl('http://svc', run, '/cam', undefined)).toBe(
      'http://svc/data/campaigns/c%201/frame?run=cfg%2Fa%2F3&topic=%2Fcam',
    )
  })
})

describe('frameIndexUrl', () => {
  it('addresses the index of one topic', () => {
    expect(frameIndexUrl('', run, '/cam')).toBe(
      '/data/campaigns/c%201/frame-index?run=cfg%2Fa%2F3&topic=%2Fcam',
    )
  })
})

describe('liveFramesUrl', () => {
  it('opens the live stream for frames only, with no tables', () => {
    expect(liveFramesUrl('', run, ['/cam', '/depth'])).toBe(
      '/data/campaigns/c%201/live?run=cfg%2Fa%2F3&tables=&frames=%2Fcam%2C%2Fdepth',
    )
  })
})

describe('screenshotUrl', () => {
  it('names the run by config and run id, the moment as at, and each view setting once', () => {
    expect(
      screenshotUrl('', run, { t: 4.25, camera: 'top', view: { azimuth: 90, distance: 12 } }),
    ).toBe(
      '/campaigns/c%201/screenshot?config_name=cfg%2Fa&run_id=3&at=4.25&camera=top' +
        '&view=azimuth%3D90&view=distance%3D12',
    )
  })

  it('asks for the newest state without at', () => {
    expect(screenshotUrl('', run)).toBe('/campaigns/c%201/screenshot?config_name=cfg%2Fa&run_id=3')
  })
})
