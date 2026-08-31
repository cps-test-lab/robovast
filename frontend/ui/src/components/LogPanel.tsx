import { useEffect, useMemo, useRef, useState } from 'react'
import ArrowDownwardRoundedIcon from '@mui/icons-material/ArrowDownwardRounded'
import Box from '@mui/material/Box'
import Fab from '@mui/material/Fab'
import Tooltip from '@mui/material/Tooltip'
import { useLiveStream } from '@/lib/liveStream'
import { containerColorer } from './containerColor'

// The one live-log renderer in the app. It lived inside StatusView, which is now only one
// of its callers: the Admin page tails the service's own log through the same component,
// and importing StatusView to get it would have dragged the whole campaign-status graph --
// BatchObjectiveChart, DetailsBox, the ETA maths -- into a lazily-loaded page that needs
// none of it.
//
// That it fits both is not a coincidence: every log this app tails is the same shape on
// the server (a `fetch(offset) -> LogChunk` behind one SSE loop), so it is the same shape
// here.

// Multi-container job logs arrive with each line tagged `[container] …` (merged
// server-side). Color only the `[container]` prefix per container; the rest of the
// line keeps the default text color. Lines without a tag render unchanged.
//
// The colours come from the container names this text actually holds, so two of them never
// share one (see containerColorer) -- which the bare hash did not guarantee.
function renderLogLines(text: string) {
  const lines = text.split('\n')
  const tags = lines.map((line) => /^(\[[^\]]+\]) ?/.exec(line))
  const color = containerColorer(tags.flatMap((m) => (m ? [m[1].slice(1, -1)] : [])))
  return lines.map((line, i) => {
    const m = tags[i]
    const nl = i > 0 ? '\n' : ''
    if (!m) return <span key={i}>{nl + line}</span>
    const prefix = m[0]
    const rest = line.slice(prefix.length)
    return (
      <span key={i}>
        {nl}
        <span style={{ color: color(m[1].slice(1, -1)) }}>{prefix}</span>
        {rest}
      </span>
    )
  })
}

/** How the *server* ended the stream, as opposed to how the transport is doing. */
type LogEnd = 'eof' | 'error' | null

// How far from the bottom still counts as "at the bottom", in px. Not zero: a sub-pixel
// scroll height (fractional line metrics, a zoomed browser) leaves a fraction of a pixel of
// slack that would read as the reader having deliberately scrolled away.
const BOTTOM_SLACK_PX = 24

// Streams a log live over Server-Sent Events (see robovast.*StreamUrl). Each delta is
// appended; the browser's own reconnect resends Last-Event-ID so the server resumes from
// the exact byte offset (no gap, no dupe), and useLiveStream covers what that reconnect
// does not — a stream the browser gave up on, and a socket that died without saying so
// while the tab was in the background. So the panel is never a silently-frozen tail: a
// blip shows `reconnecting…` and heals itself; a server-side application error (e.g. pod
// gone, no durable copy) shows verbatim; a terminal log ends cleanly on `eof`. `resetKey`
// restarts the stream when the source changes; callers gate visibility by mounting.
export function LogPanel({ resetKey, streamUrl }: { resetKey: string; streamUrl: string }) {
  const [text, setText] = useState('')
  const [end, setEnd] = useState<LogEnd>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const preRef = useRef<HTMLPreElement>(null)
  // The tail follows the newest line only while the reader is *at* the newest line. Starts true
  // so an opening panel shows the end of the log, which is what a tail is for.
  const [following, setFollowing] = useState(true)

  const { state, finish, generation } = useLiveStream(streamUrl, {
    resetKey,
    onMessage: (e) => {
      try {
        const delta = JSON.parse(e.data) as string
        if (delta) setText((t) => t + delta)
      } catch {
        /* malformed frame — ignore rather than break the tail */
      }
    },
    events: {
      // Application error the server chose to surface (pod gone, upload missing, …).
      streamerror: (e) => {
        try {
          setErrorMsg(JSON.parse(e.data) as string)
        } catch {
          setErrorMsg('log stream error')
        }
        setEnd('error')
        finish()
      },
      // Terminal log — nothing more will ever be written. Closing it deliberately also
      // tells the watchdog not to treat the silence that follows as a fault.
      eof: () => {
        setEnd((e) => (e === 'error' ? e : 'eof'))
        finish()
      },
    },
  })

  // A connection this component opened starts the log from byte zero (Last-Event-ID is the
  // browser's to send, not ours), so the text it is about to re-send has to go first — or
  // the whole log would appear twice. The browser's own reconnect does not bump the
  // generation and correctly keeps what is on screen.
  useEffect(() => {
    setText('')
    setEnd(null)
    setErrorMsg(null)
    // A different log (or one restarted from byte zero) is a new thing to read from its end.
    setFollowing(true)
  }, [generation, resetKey, streamUrl])

  const lines = useMemo(() => (text ? renderLogLines(text) : null), [text])

  // Footer status shown under the log body: nothing while healthily streaming, an
  // explicit note while reconnecting / errored / (when empty) connecting or ended.
  // An open stream with nothing in it is not loading — the connection is up and the
  // source has produced no bytes (a job whose containers have not started writing yet;
  // PodLogTail swallows the API's 400 for a container with no log). Saying `loading…`
  // there promised output that nothing was on its way to deliver.
  const footer =
    end === 'error'
      ? `stream error: ${errorMsg ?? 'unknown'}`
      : end !== 'eof' && (state === 'reconnecting' || state === 'closed')
        ? 'reconnecting…'
        : !lines
          ? end === 'eof'
            ? '(no log)'
            : state === 'open'
              ? '(no output yet)'
              : 'loading…'
          : null

  // Stick to the bottom only while following, and never out from under a selection: a scroll
  // mid-drag loses the anchor, which makes a live log impossible to copy from. Holding a
  // selection does not clear `following` either, so dropping it resumes the tail by itself —
  // same contract as RunLogView's follow mode.
  //
  // `footer` is a dependency as much as the text is: `reconnecting…` appearing under the last
  // line grows the body exactly like one more log line.
  useEffect(() => {
    const el = preRef.current
    if (!el || !following) return
    const sel = window.getSelection()
    const held = !!sel && !sel.isCollapsed && !!sel.anchorNode && el.contains(sel.anchorNode)
    if (held) return
    el.scrollTop = el.scrollHeight
  }, [text, footer, following])

  // Appending below the viewport moves nothing and fires no scroll event, so every scroll that
  // arrives here is the reader's — or this panel's own jump, which lands at the bottom and so
  // correctly re-arms following.
  const onScroll = () => {
    const el = preRef.current
    if (!el) return
    setFollowing(el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK_PX)
  }

  return (
    <Box sx={{ position: 'relative' }}>
      <Box
        component="pre"
        ref={preRef}
        onScroll={onScroll}
        sx={{
          m: 0,
          px: 1,
          py: 0.75,
          // Darker than the card it sits in, and with no border of its own: it is always
          // mounted inside a CollapsibleBox body, which supplies the frame.
          bgcolor: 'background.default',
          color: 'text.primary',
          fontFamily: 'monospace',
          fontSize: '0.72rem',
          whiteSpace: 'pre-wrap',
          overflowX: 'auto',
          maxHeight: 320,
          overflowY: 'auto',
        }}
      >
        {lines}
        {footer ? (
          <Box
            component="span"
            sx={{
              display: 'block',
              color: end === 'error' ? 'error.main' : 'text.secondary',
              opacity: 0.85,
            }}
          >
            {lines ? '\n' : ''}
            {footer}
          </Box>
        ) : null}
      </Box>

      {/* The only visible sign that the tail was paused, and the way back. Shown only once the
          reader has actually left the bottom of a log that has something in it — a panel that
          fits its log entirely never scrolls, so it never detaches and never grows a button. */}
      {!following && lines ? (
        <Tooltip title="Jump to the latest line and resume following">
          <Fab
            size="small"
            color="primary"
            aria-label="jump to the latest log line"
            onClick={() => {
              const el = preRef.current
              if (el) el.scrollTop = el.scrollHeight
              setFollowing(true)
            }}
            sx={{ position: 'absolute', right: 14, bottom: 10, zIndex: 2 }}
          >
            <ArrowDownwardRoundedIcon fontSize="small" />
          </Fab>
        </Tooltip>
      ) : null}
    </Box>
  )
}
