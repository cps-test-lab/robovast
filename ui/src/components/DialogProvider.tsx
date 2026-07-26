import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import Button from '@mui/material/Button'
import Dialog from '@mui/material/Dialog'
import DialogActions from '@mui/material/DialogActions'
import DialogContent from '@mui/material/DialogContent'
import DialogContentText from '@mui/material/DialogContentText'
import DialogTitle from '@mui/material/DialogTitle'
import TextField from '@mui/material/TextField'

// The single, app-wide way to ask the user for something. A provider mounts one MUI Dialog and
// hands out an imperative, promise-based API via useDialogs():
//   const { prompt, confirm } = useDialogs()
//   const name = await prompt({ title: 'New .vast file', label: 'File name', defaultValue: 'config.vast' })
//   const ok = await confirm({ title: 'Overwrite scenario.osc?', message: '…' })
// prompt resolves to the entered string (or null on cancel); confirm resolves to a boolean. One
// dialog at a time — a second call while one is open rejects the earlier one (keeps this minimal).

export interface PromptOptions {
  title: string
  label?: string
  message?: string
  defaultValue?: string
  confirmLabel?: string
  placeholder?: string
  /** Return an error string to block submission, or null/undefined when the value is acceptable. */
  validate?: (value: string) => string | null | undefined
}

export interface ConfirmOptions {
  title: string
  message?: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  /** Style the confirm button as a destructive action. */
  danger?: boolean
}

/** One button in a `choose` dialog: its `value` is what the promise resolves to. */
export interface Choice {
  label: string
  value: string
  color?: 'primary' | 'error' | 'inherit'
  /** Render as the filled/primary button (the default action). */
  primary?: boolean
}

export interface ChooseOptions {
  title: string
  message?: ReactNode
  choices: Choice[]
}

interface DialogsApi {
  prompt: (opts: PromptOptions) => Promise<string | null>
  confirm: (opts: ConfirmOptions) => Promise<boolean>
  /** Ask the user to pick one of N buttons; resolves to the chosen `value` (or null on Esc/close). */
  choose: (opts: ChooseOptions) => Promise<string | null>
}

const DialogsContext = createContext<DialogsApi | null>(null)

// Discriminated state of the currently-open dialog (or null when idle).
type ActiveState =
  | { kind: 'prompt'; opts: PromptOptions; resolve: (v: string | null) => void }
  | { kind: 'confirm'; opts: ConfirmOptions; resolve: (v: boolean) => void }
  | { kind: 'choose'; opts: ChooseOptions; resolve: (v: string | null) => void }
  | null

export function DialogProvider({ children }: { children: ReactNode }) {
  const [active, setActive] = useState<ActiveState>(null)
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  // Latest resolve, so closing helpers always settle the pending promise exactly once.
  const resolveRef = useRef<((v: never) => void) | null>(null)

  const settle = useCallback((v: unknown) => {
    resolveRef.current?.(v as never)
    resolveRef.current = null
    setActive(null)
    setError(null)
  }, [])

  const prompt = useCallback(
    (opts: PromptOptions) =>
      new Promise<string | null>((resolve) => {
        resolveRef.current?.(null as never) // supersede any open dialog
        resolveRef.current = resolve as (v: never) => void
        setValue(opts.defaultValue ?? '')
        setError(null)
        setActive({ kind: 'prompt', opts, resolve })
      }),
    [],
  )

  const confirm = useCallback(
    (opts: ConfirmOptions) =>
      new Promise<boolean>((resolve) => {
        resolveRef.current?.(false as never)
        resolveRef.current = resolve as (v: never) => void
        setActive({ kind: 'confirm', opts, resolve })
      }),
    [],
  )

  const choose = useCallback(
    (opts: ChooseOptions) =>
      new Promise<string | null>((resolve) => {
        resolveRef.current?.(null as never)
        resolveRef.current = resolve as (v: never) => void
        setActive({ kind: 'choose', opts, resolve })
      }),
    [],
  )

  const api = useMemo<DialogsApi>(() => ({ prompt, confirm, choose }), [prompt, confirm, choose])

  const submitPrompt = () => {
    if (active?.kind !== 'prompt') return
    const v = value.trim()
    const err = active.opts.validate?.(v)
    if (err) {
      setError(err)
      return
    }
    settle(v)
  }

  return (
    <DialogsContext.Provider value={api}>
      {children}
      <Dialog
        open={active !== null}
        onClose={() => settle(active?.kind === 'confirm' ? false : null)}
        maxWidth="xs"
        fullWidth
        // Submit on Enter for the prompt input; MUI already closes on Esc via onClose.
        onKeyDown={(e) => {
          if (e.key === 'Enter' && active?.kind === 'prompt') {
            e.preventDefault()
            submitPrompt()
          }
        }}
      >
        <DialogTitle>{active?.opts.title}</DialogTitle>
        <DialogContent>
          {active?.kind === 'prompt' ? (
            <>
              {active.opts.message ? (
                <DialogContentText sx={{ mb: 2 }}>{active.opts.message}</DialogContentText>
              ) : null}
              <TextField
                autoFocus
                fullWidth
                size="small"
                variant="outlined"
                label={active.opts.label}
                placeholder={active.opts.placeholder}
                value={value}
                error={!!error}
                helperText={error ?? ' '}
                onChange={(e) => {
                  setValue(e.target.value)
                  if (error) setError(null)
                }}
              />
            </>
          ) : active?.kind === 'confirm' || active?.kind === 'choose' ? (
            <DialogContentText>{active.opts.message}</DialogContentText>
          ) : null}
        </DialogContent>
        <DialogActions>
          {active?.kind === 'choose' ? (
            // Fully caller-defined button set; each resolves to its own value.
            active.opts.choices.map((c) => (
              <Button
                key={c.value}
                onClick={() => settle(c.value)}
                variant={c.primary ? 'contained' : 'text'}
                color={c.color ?? (c.primary ? 'primary' : 'inherit')}
              >
                {c.label}
              </Button>
            ))
          ) : (
            <>
              <Button onClick={() => settle(active?.kind === 'confirm' ? false : null)} color="inherit">
                {active?.kind === 'confirm' ? active.opts.cancelLabel ?? 'Cancel' : 'Cancel'}
              </Button>
              {active?.kind === 'prompt' ? (
                <Button onClick={submitPrompt} variant="contained">
                  {active.opts.confirmLabel ?? 'OK'}
                </Button>
              ) : active?.kind === 'confirm' ? (
                <Button
                  onClick={() => settle(true)}
                  variant="contained"
                  color={active.opts.danger ? 'error' : 'primary'}
                >
                  {active.opts.confirmLabel ?? 'Confirm'}
                </Button>
              ) : null}
            </>
          )}
        </DialogActions>
      </Dialog>
    </DialogsContext.Provider>
  )
}

export function useDialogs(): DialogsApi {
  const ctx = useContext(DialogsContext)
  if (!ctx) throw new Error('useDialogs must be used within a <DialogProvider>')
  return ctx
}
