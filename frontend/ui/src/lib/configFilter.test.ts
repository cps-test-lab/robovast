import { describe, expect, it } from 'vitest'
import { filterPatterns, globToRegExp, isGlob, matchConfigs, matchesPattern } from './configFilter'

const names = ['config1-1-1', 'config1-1-2', 'config2-1-1', 'hall.a']

describe('configFilter', () => {
  it('splits comma-separated globs and drops blanks', () => {
    expect(filterPatterns(' a*, ,b ')).toEqual(['a*', 'b'])
  })

  it('selects a name any pattern matches', () => {
    expect(matchConfigs(names, 'config1-1-2,config2-*')).toEqual(['config1-1-2', 'config2-1-1'])
  })

  it('selects everything for an empty filter', () => {
    expect(matchConfigs(names, '')).toEqual(names)
  })

  it('matches the whole name, as fnmatch does', () => {
    expect(matchConfigs(names, 'config1')).toEqual([])
    expect(matchConfigs(names, 'config1*')).toEqual(['config1-1-1', 'config1-1-2'])
  })

  it('treats regex metacharacters literally and is case-sensitive', () => {
    expect(matchesPattern('hall.a', 'hall.a')).toBe(true)
    expect(matchesPattern('hallxa', 'hall.a')).toBe(false)
    expect(matchesPattern('Hall.a', 'hall.a')).toBe(false)
  })

  it('reads ?, [seq] and [!seq] as fnmatch does', () => {
    expect(matchConfigs(names, 'config?-1-1')).toEqual(['config1-1-1', 'config2-1-1'])
    expect(matchConfigs(names, 'config[2]-*')).toEqual(['config2-1-1'])
    expect(matchConfigs(names, 'config[!2]-1-1')).toEqual(['config1-1-1'])
  })

  it('keeps an unclosed [ literal', () => {
    expect(globToRegExp('a[b').test('a[b')).toBe(true)
  })

  it('tells a glob from a name', () => {
    expect(isGlob('config1-1-1')).toBe(false)
    expect(isGlob('config1*')).toBe(true)
  })
})
