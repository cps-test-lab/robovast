import { createTheme } from '@mui/material/styles'

// Teal/amber on near-black — a shared visual identity across our web UIs, not shared code.
export const appTheme = createTheme({
  palette: {
    mode: 'dark',
    primary: { main: '#2dd4bf' },
    secondary: { main: '#f0b429' },
    background: { default: '#101316', paper: 'rgba(18, 24, 28, 0.9)' },
    success: { main: '#4ade80' },
    warning: { main: '#f0b429' },
    error: { main: '#f48fb1' },
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
          border: '1px solid rgba(255, 255, 255, 0.08)',
        },
      },
    },
  },
})
