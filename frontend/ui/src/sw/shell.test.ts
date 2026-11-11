// The service worker's policy: what it serves from the cache, what it loads from the network, and
// what it never touches.
import { describe, expect, it } from 'vitest'
import { CACHE_PREFIX, cacheName, route, staleCaches } from './shell'

const ORIGIN = 'https://robovast.example.com'
const PRECACHE = new Set(['/assets/index-abc123.js', '/assets/index-def456.css', '/manifest.webmanifest'])
const get = (path: string, mode = 'cors') => ({ url: `${ORIGIN}${path}`, method: 'GET', mode })

describe('route', () => {
  it('serves exactly the shell files from the cache', () => {
    expect(route(get('/assets/index-abc123.js'), ORIGIN, PRECACHE)).toBe('shell')
    expect(route(get('/manifest.webmanifest'), ORIGIN, PRECACHE)).toBe('shell')
  })

  it('loads every page from the network, the start page included', () => {
    expect(route(get('/', 'navigate'), ORIGIN, PRECACHE)).toBe('page')
    expect(route(get('/login?next=/', 'navigate'), ORIGIN, PRECACHE)).toBe('page')
    // Even a shell URL, opened as a page, is a page load.
    expect(route(get('/assets/index-abc123.js', 'navigate'), ORIGIN, PRECACHE)).toBe('page')
  })

  it('never touches the API, run data, streams, the login or a lazily loaded chunk', () => {
    for (const path of [
      '/campaigns?limit=200&offset=0',
      '/campaigns/events',
      '/campaigns/demo/logs/stream',
      '/results/demo/config-1/0/scene.glb',
      '/data/demo.tar',
      '/sources/ws/project.vast',
      '/usage',
      '/version',
      '/login',
      '/assets/monaco-xyz.js', // built, but not part of the shell
      '/',                     // the start page as a plain fetch is not a shell file either
    ]) {
      expect(route(get(path), ORIGIN, PRECACHE), path).toBe('pass')
    }
  })

  it('never touches a POST, even to a shell URL, or another origin', () => {
    expect(route({ url: `${ORIGIN}/login`, method: 'POST', mode: 'navigate' }, ORIGIN, PRECACHE)).toBe('pass')
    expect(route({ ...get('/assets/index-abc123.js'), method: 'POST' }, ORIGIN, PRECACHE)).toBe('pass')
    expect(route({ url: 'https://other.example.com/assets/index-abc123.js', method: 'GET', mode: 'cors' },
      ORIGIN, PRECACHE)).toBe('pass')
  })

  it('does not serve a shell URL that carries a query', () => {
    expect(route(get('/assets/index-abc123.js?v=2'), ORIGIN, PRECACHE)).toBe('pass')
  })
})

describe('staleCaches', () => {
  it('drops every shell cache but this build\'s, and leaves caches it does not own alone', () => {
    const keys = [cacheName('old1'), cacheName('new'), cacheName('old2'), 'someone-else', `x${CACHE_PREFIX}`]
    expect(staleCaches(keys, 'new')).toEqual([cacheName('old1'), cacheName('old2')])
  })
})
