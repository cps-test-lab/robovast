import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { decided, optedIn, post, setOptedIn, shouldAsk, supported } from './browserNotify'

// There is no jsdom here (the suite is pure logic, see vite.config.ts), so the two globals this
// module reads are stood up by hand. Both are read at call time, never at import time, which is
// what makes swapping them per-test work at all.

const KEY = 'robovast.browserNotifications'

interface Stub {
  store: Map<string, string>
  notifications: Array<{ title: string; options?: NotificationOptions }>
}

let stub: Stub

/** Install a window with localStorage + Notification, and a document visibility. */
function install(opts: {
  permission?: NotificationPermission
  visibility?: DocumentVisibilityState
  /** Storage that throws on every access, as private modes and blocked site data do. */
  hostileStorage?: boolean
  /** Omit Notification entirely: plain http, and some embeddings. */
  unsupported?: boolean
} = {}) {
  stub = { store: new Map(), notifications: [] }
  const storage = opts.hostileStorage
    ? {
      getItem: () => { throw new Error('blocked') },
      setItem: () => { throw new Error('blocked') },
    }
    : {
      getItem: (k: string) => stub.store.get(k) ?? null,
      setItem: (k: string, v: string) => { stub.store.set(k, v) },
    }

  const NotificationStub = function (this: unknown, title: string, options?: NotificationOptions) {
    stub.notifications.push({ title, options })
  } as unknown as typeof Notification
  ;(NotificationStub as { permission: NotificationPermission }).permission =
    opts.permission ?? 'default'

  const win: Record<string, unknown> = { localStorage: storage }
  if (!opts.unsupported) win.Notification = NotificationStub

  vi.stubGlobal('window', win)
  vi.stubGlobal('Notification', opts.unsupported ? undefined : NotificationStub)
  vi.stubGlobal('document', { visibilityState: opts.visibility ?? 'hidden' })
}

beforeEach(() => install())
afterEach(() => vi.unstubAllGlobals())

describe('the stored preference', () => {
  it('is three states, and absent is the one that means "never asked"', () => {
    expect(decided()).toBe(false)
    expect(optedIn()).toBe(false)

    setOptedIn(false)
    expect(stub.store.get(KEY)).toBe('0')
    // The point of storing the decline: it is answered, not merely off.
    expect(decided()).toBe(true)
    expect(optedIn()).toBe(false)

    setOptedIn(true)
    expect(stub.store.get(KEY)).toBe('1')
    expect(decided()).toBe(true)
    expect(optedIn()).toBe(true)
  })

  it('reports "decided" when storage throws, so the ask is skipped rather than repeated', () => {
    install({ hostileStorage: true })
    expect(decided()).toBe(true)
    expect(optedIn()).toBe(false)
    expect(shouldAsk()).toBe(false)
    // Failing to remember must not throw out of the caller.
    expect(() => setOptedIn(true)).not.toThrow()
  })
})

describe('shouldAsk', () => {
  it('asks an undecided browser whose permission is still default', () => {
    expect(shouldAsk()).toBe(true)
  })

  it('asks an undecided browser that already granted: the grant is not the wanting', () => {
    install({ permission: 'granted' })
    expect(shouldAsk()).toBe(true)
  })

  it('never asks after a denial, which cannot be re-prompted from the page', () => {
    install({ permission: 'denied' })
    expect(shouldAsk()).toBe(false)
  })

  it('never asks twice, whichever way the first answer went', () => {
    setOptedIn(true)
    expect(shouldAsk()).toBe(false)
    setOptedIn(false)
    expect(shouldAsk()).toBe(false)
  })

  it('does not ask where notifications do not exist', () => {
    install({ unsupported: true })
    expect(supported()).toBe(false)
    expect(shouldAsk()).toBe(false)
  })
})

describe('post', () => {
  const note = { title: 'Campaign finished', body: 'abc', tag: 'abc' }

  it('posts when opted in, granted, and the tab is hidden', () => {
    install({ permission: 'granted' })
    setOptedIn(true)
    post(note)
    expect(stub.notifications).toEqual([
      { title: 'Campaign finished', options: { body: 'abc', tag: 'abc' } },
    ])
  })

  it('stays silent on a visible tab, which already showed the toast', () => {
    install({ permission: 'granted', visibility: 'visible' })
    setOptedIn(true)
    post(note)
    expect(stub.notifications).toEqual([])
  })

  it('stays silent without the opt-in, even with permission granted', () => {
    install({ permission: 'granted' })
    post(note)
    expect(stub.notifications).toEqual([])
  })

  it('stays silent without permission, even when opted in', () => {
    setOptedIn(true)
    post(note)
    expect(stub.notifications).toEqual([])
  })

  it('swallows a constructor that throws, as platforms wanting a service worker do', () => {
    install({ permission: 'granted' })
    setOptedIn(true)
    const throwing = function () { throw new Error('needs a service worker') } as unknown as typeof Notification
    ;(throwing as { permission: NotificationPermission }).permission = 'granted'
    vi.stubGlobal('Notification', throwing)
    expect(() => post(note)).not.toThrow()
  })
})
