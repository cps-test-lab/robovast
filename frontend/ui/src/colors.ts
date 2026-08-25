// The RoboVAST web UI colour scheme.
//
// ── The four groups ───────────────────────────────────────────────────────────────────────────
// Which group a colour is in decides whether it may move when the brand does:
//
//   surface  the ground. Neutral dark grey, deliberately NOT the marketing site's near-black
//            green: this is a tool that stays open for hours in front of long log tails, 3D
//            scenes and charts, and on a tinted ground every one of those picks up the tint.
//            A colourless ground leaves the accent and the status colours as the only hues on
//            screen, which is what makes them read as meaning something.
//   brand    the accent, from the site's palette. The identity, and the one thing shared with
//            the page that introduces the product. Transcribed from that site's CSS custom
//            properties rather than imported: it is a static page with no build step and no
//            package to depend on, and a shared palette package would couple a deployed
//            service's bundle to a website.
//   status   what a phase chip, a meter, a tree node or a log line is *saying*. Semantics, not
//            identity: they must stay put when the brand moves, or a re-brand silently re-labels
//            every campaign. Held to one lightness band so no single status shouts, with the
//            hues kept meaningful (green good / amber attention / rose bad / blue working).
//   data     categorical series colours. An encoding, not a set of brand tints: series 3 is this
//            colour because series 2 was that one. Kept in a *brighter* register than status on
//            purpose — status is painted as filled chips and bars, where a deep tone reads well,
//            while a series is a 1px line that has to survive being thin.
//
// ── Preparing for switchable styles ───────────────────────────────────────────────────────────
// There is one style today and `ACTIVE` is it. The shape is here so a second one is a new object
// rather than a refactor: implement `Style`, and `buildTheme` in theme.ts turns it into an MUI
// theme. What is deliberately NOT built yet is *runtime* switching — the flat tokens below are
// module constants resolved at build time, which is what lets a canvas-painting panel read a
// colour without a hook. Making the style switchable while the app runs needs the panels to read
// from context (`useTheme()`) instead of these constants, and every token a panel uses to live on
// the theme. That is the whole delta; nothing here blocks it.
//
// Import a token from here instead of writing a hex. Before this module the surfaces were spread
// across theme.ts and two components, the panel canvas colour was copied into six files, the
// series scale existed twice — with a comment on one copy asking the other to stay in sync — and
// the scenario tree and the log view each had their own private green and red.

/** Everything a style must define. Sub-grouped to match the four groups above. */
export interface Style {
  /** Names the style in a future picker, and in a bug report. */
  readonly name: string
  readonly surface: {
    readonly bg: string
    /** Channels, not a hex: Paper is translucent over a blur, so it is only used at an alpha. */
    readonly paperRgb: string
    /** Behind a panel that paints its own pixels. Opaque — these draw every frame, and
     *  compositing them over the glass Paper is a blur the GPU redoes for nothing. */
    readonly canvas: string
    /** Hairlines: the Paper border, dividers. */
    readonly line: string
  }
  readonly brand: {
    readonly accent: string
    /** Channels, because the accent is mostly used at some alpha — see `accent()`. */
    readonly accentRgb: string
    readonly accentLight: string
    readonly accentDark: string
    /** Text drawn *on* an accent fill. */
    readonly onAccent: string
  }
  readonly text: {
    readonly ink: string
    readonly muted: string
    readonly faint: string
  }
  readonly status: {
    readonly success: string
    readonly warning: string
    readonly error: string
    readonly info: string
    /** A status that is defined but says nothing — a tree node not ticked yet. */
    readonly neutral: string
  }
  readonly data: {
    /** In order, and the order is load-bearing: append rather than reorder. */
    readonly series: readonly string[]
    readonly grid: string
    readonly axis: string
  }
  readonly scene: {
    /** The 3D ground grid's axis lines, and its ordinary lines. Opaque hexes rather than white at
     *  an alpha like `data.grid` / `data.axis`: they are painted by a three LineBasicMaterial over
     *  `surface.canvas`, so a style keeping the grid in family with every other hairline in the UI
     *  states the *blend* here — the same reading, without a per-frame alpha composite. */
    readonly gridCenter: string
    readonly grid: string
    /** A scene marker naming no colour of its own. Outside the accent's family on purpose — a
     *  marker is an annotation laid *over* the world and must not read as part of it. */
    readonly marker: string
    /** "The value or position being pointed at": the 2D playhead dot, a preview's resolved-value
     *  rule. Its own role rather than a reuse of `accent` so a style can separate the two. */
    readonly mark: string
  }
}

/** Mint on neutral dark grey. The only style so far. */
export const midnightMint: Style = {
  name: 'Midnight Mint',
  surface: {
    bg: '#101316',
    paperRgb: '18, 24, 28',
    canvas: '#12171f',
    line: 'rgba(255, 255, 255, 0.08)',
  },
  brand: {
    accent: '#a8ffcf', //      --mint
    accentRgb: '168, 255, 207',
    accentLight: '#c3ffdc', // the site's primary-button hover
    accentDark: '#74ffb5', //  --mint-bright
    onAccent: '#05100c', //    the site's text colour on a mint fill
  },
  // The site's text ramp: a hair warmer than MUI's white-at-alpha default, and the three levels
  // are spaced deliberately rather than at 100/70/50 percent of white.
  text: {
    ink: '#f3f7f4', //   --ink
    muted: '#9bacaa', // --muted
    faint: '#657573', // --faint
  },
  // One lightness band, so a screen full of chips has no single colour jumping out of it, and all
  // four sit clearly *below* the accent — which is what keeps the light mint reading as the brand
  // rather than as a fifth status.
  status: {
    success: '#2f9e6e',
    warning: '#c9902e',
    error: '#c9556c',
    info: '#3d87b8',
    neutral: '#657573',
  },
  data: {
    series: ['#2dd4bf', '#f0b429', '#4ade80', '#f48fb1', '#60a5fa', '#c084fc'],
    grid: 'rgba(255, 255, 255, 0.08)',
    axis: 'rgba(255, 255, 255, 0.2)',
  },
  scene: {
    // data.axis and data.grid (white at 20% / 8%) composited over surface.canvas: the 3D grid reads
    // as the same hairline as a chart's, and the scene keeps the accent and the status colours as
    // the only hues in it.
    gridCenter: '#41454c',
    grid: '#252a31',
    marker: '#38bdf8',
    mark: '#a8ffcf',
  },
}

/** The style the app is built with. Swapping this line swaps the whole UI. */
export const ACTIVE: Style = midnightMint

// ── Tokens ────────────────────────────────────────────────────────────────────────────────────
// The flat names the app imports. Derived from ACTIVE so a style swap needs no edits here.

export const BG = ACTIVE.surface.bg
export const SURFACE_RGB = ACTIVE.surface.paperRgb
export const CANVAS = ACTIVE.surface.canvas
export const LINE = ACTIVE.surface.line

export const ACCENT = ACTIVE.brand.accent
export const ACCENT_LIGHT = ACTIVE.brand.accentLight
export const ACCENT_BRIGHT = ACTIVE.brand.accentDark
export const ON_ACCENT = ACTIVE.brand.onAccent

export const INK = ACTIVE.text.ink
export const MUTED = ACTIVE.text.muted
export const FAINT = ACTIVE.text.faint

export const SUCCESS = ACTIVE.status.success
export const WARNING = ACTIVE.status.warning
export const ERROR = ACTIVE.status.error
export const INFO = ACTIVE.status.info
export const NEUTRAL = ACTIVE.status.neutral

export const SERIES = ACTIVE.data.series
export const CHART_GRID = ACTIVE.data.grid
export const CHART_AXIS = ACTIVE.data.axis
/** Chart text comes from the ramp, so a chart label matches every other secondary label. */
export const CHART_TITLE = ACTIVE.text.ink
export const CHART_LABEL = ACTIVE.text.muted

export const GRID_CENTER = ACTIVE.scene.gridCenter
export const GRID = ACTIVE.scene.grid
export const MARKER_DEFAULT = ACTIVE.scene.marker
export const MARK = ACTIVE.scene.mark

// ── Helpers ───────────────────────────────────────────────────────────────────────────────────

/** The accent at some alpha — selection fills, the brand mark's cells, a focus tint. A function
 *  rather than a handful of named tints because callers want different weights and each one
 *  otherwise re-states the channels. */
export function accent(alpha: number): string {
  return `rgba(${ACTIVE.brand.accentRgb}, ${alpha})`
}

/** A scheme colour at some alpha — a fill under a line, a wash behind a mark. Takes the 6-digit
 *  hex the tokens are written as; the accent has `accent()` because it is used this way often
 *  enough to be worth its own name. */
export function withAlpha(hex: string, alpha: number): string {
  const n = Number.parseInt(hex.slice(1), 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}
