import { createTheme, type Theme } from '@mui/material/styles'
import { ACTIVE, type Style } from './colors'
import { ON_COARSE, TOUCH_TARGET_PX } from './lib/compact'

// Under a finger every target is at least TOUCH_TARGET_PX square. Media queries rather than a theme
// built per device: one theme, and a mouse keeps the compact desktop density it was designed at.
const COARSE = ON_COARSE
const touchSize = { [COARSE]: { minWidth: TOUCH_TARGET_PX, minHeight: TOUCH_TARGET_PX } }
const touchHeight = { [COARSE]: { minHeight: TOUCH_TARGET_PX } }

// For a target whose drawn size is part of what it says -- a status chip in a row, a checkbox in a
// table -- the hit area grows instead of the drawing: an invisible square of TOUCH_TARGET_PX, centred
// on it, belongs to the element and takes the tap. Rows keep their height.
const touchHitArea = {
  [COARSE]: {
    position: 'relative',
    '&::after': {
      content: '""',
      position: 'absolute',
      top: '50%',
      left: '50%',
      width: `max(100%, ${TOUCH_TARGET_PX}px)`,
      height: `max(100%, ${TOUCH_TARGET_PX}px)`,
      transform: 'translate(-50%, -50%)',
    },
  },
}

// The MUI half of the colour scheme: maps a `Style` onto the roles MUI paints from
// (palette.primary, background.paper, text.secondary, ...), so everything a component reaches for
// as `'primary.main'` or `'error.main'` is decided here. The values, and why each is what it is,
// live in colors.ts.
//
// Taking the style as an argument rather than reading the tokens directly is the seam a second
// style needs: `buildTheme(otherStyle)` is the whole of it. See the style note in colors.ts for
// what runtime switching would additionally require.
export function buildTheme(style: Style): Theme {
  const { surface, brand, text, status } = style
  return createTheme({
    palette: {
      mode: 'dark',
      // `contrastText` is pinned rather than left to MUI's auto-pick: the accent is light enough
      // that the automatic choice is a pure black the scheme does not contain — the site fills
      // its mint buttons with the near-black end of its own palette instead. The status colours
      // below are left to the auto-pick on purpose: they sit in one lightness band that straddles
      // the readable-on-white threshold, and MUI picks per colour better than a blanket rule
      // (white on the green/rose/blue, dark on the amber).
      primary: {
        main: brand.accent,
        light: brand.accentLight,
        dark: brand.accentDark,
        contrastText: brand.onAccent,
      },
      // Functional rather than brand: the peak tick on a demand chart, the "attention, not
      // failure" accent. Hence the same amber as `warning`.
      secondary: { main: status.warning },
      background: { default: surface.bg, paper: `rgba(${surface.paperRgb}, 0.9)` },
      divider: surface.line,
      text: { primary: text.ink, secondary: text.muted, disabled: text.faint },
      success: { main: status.success },
      warning: { main: status.warning },
      error: { main: status.error },
      info: { main: status.info },
    },
    shape: { borderRadius: 8 },
    typography: {
      fontFamily: '"Roboto", "Helvetica", "Arial", sans-serif',
      h6: { fontWeight: 700, letterSpacing: 0 },
      subtitle2: { textTransform: 'uppercase', letterSpacing: 0, fontWeight: 700 },
    },
    components: {
      MuiIconButton: { styleOverrides: { root: touchSize } },
      MuiButton: { styleOverrides: { root: touchHeight } },
      MuiToggleButton: { styleOverrides: { root: touchSize } },
      MuiTab: { styleOverrides: { root: touchHeight } },
      MuiListItemButton: { styleOverrides: { root: touchHeight } },
      // A text field is hit anywhere on its box, which focuses the input inside.
      MuiInputBase: { styleOverrides: { root: touchHeight } },
      MuiChip: { styleOverrides: { clickable: touchHitArea, deletable: touchHitArea } },
      MuiCheckbox: { styleOverrides: { root: touchHitArea } },
      MuiSwitch: { styleOverrides: { switchBase: touchHitArea } },
      MuiRadio: { styleOverrides: { root: touchHitArea } },
      MuiTooltip: {
        // Both props apply to touch only, so a mouse opens a tip by hovering. A tap shows the tip
        // at once and it stays long enough to read: what a hover reveals is one tap away, not
        // behind a long press nobody knows to try.
        defaultProps: { enterTouchDelay: 0, leaveTouchDelay: 4000 },
      },
      MuiPaper: {
        styleOverrides: {
          root: {
            backdropFilter: 'blur(16px)',
            backgroundImage: 'none',
            border: `1px solid ${surface.line}`,
          },
        },
      },
    },
  })
}

export const appTheme = buildTheme(ACTIVE)
