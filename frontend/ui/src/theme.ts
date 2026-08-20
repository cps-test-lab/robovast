import { createTheme, type Theme } from '@mui/material/styles'
import { ACTIVE, type Style } from './colors'

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
