import { useEffect, useMemo, useRef, useState } from 'react'
import ArrowDownwardRoundedIcon from '@mui/icons-material/ArrowDownwardRounded'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import Fab from '@mui/material/Fab'
import Tooltip from '@mui/material/Tooltip'
import { useLiveStream, type LiveState } from '@/lib/liveStream'
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

/**
 * The note under the log body, and how to dress it: `busy` earns a spinner, `error` the
 * error colour, `note` neither. `null` while a healthy stream is delivering — a tail that
 * is working says nothing.
 */
export type LogFooter = { text: string; kind: 'busy' | 'error' | 'note' } | null

/**
 * What to say under an empty (or troubled) log.
 *
 * The distinction that matters is between an empty log that is *known* to be empty and one
 * nobody has read yet. `state === 'open'` does not draw it: the server flushes the response
 * headers before its first pull, so the socket is open — and the panel would claim "(no
 * output yet)" — for however long that pull takes, which across a cluster is tens of
 * seconds. `received` is the server having actually spoken (a delta, or a `heartbeat`
 * meaning "read it, there was nothing"), and only then is the emptiness a fact about the
 * log rather than about our own latency.
 *
 * "(no output yet)" is still the right answer once it *is* a fact — a job whose containers
 * have not started writing has an open stream and no bytes, and PodLogTail swallows the
 * API's 400 for a container with no log — so it is kept, just no longer said blind.
 */
export function logFooter(o: {
  end: LogEnd
  errorMsg: string | null
  state: LiveState
  received: boolean
  /** Nothing has been rendered into the body. */
  empty: boolean
}): LogFooter {
  if (o.end === 'error') {
    return { text: `stream error: ${o.errorMsg ?? 'unknown'}`, kind: 'error' }
  }
  if (o.end !== 'eof' && (o.state === 'reconnecting' || o.state === 'closed')) {
    return { text: 'reconnecting…', kind: 'busy' }
  }
  if (!o.empty) return null
  if (o.end === 'eof') return { text: '(no log)', kind: 'note' }
  if (o.state === 'open' && o.received) return { text: '(no output yet)', kind: 'note' }
  return { text: 'loading…', kind: 'busy' }
}

// The tail the panel keeps. What this bounds is not memory but the cost of *every* poll:
// the body is rebuilt from the whole buffer on each delta (a span per line, plus the
// container-tag scan), so an unbounded buffer makes a campaign log of tens of megabytes
// re-render hundreds of thousands of spans twice a second, and the panel stops responding
// to its own scrollbar. The server caps what one frame carries; this caps what accumulates
// across them. Generous next to the ~320px window it is read through — enough that
// scrolling back a long way still works.
const KEEP_CHARS = 512 * 1024

// What replaces the head once it is dropped. A truncation the reader cannot see is a log
// that lies about what the campaign printed.
const HEAD_DROPPED = '[… earlier output not shown …]'

/**
 * Keep the last {@link KEEP_CHARS} of `text`, from a line boundary, marked as trimmed.
 *
 * Trimming from the front is what makes this safe to apply on every append: the marker
 * it leaves is itself part of the head, so a later trim drops it along with the rest and
 * re-adds exactly one — the panel never accumulates a stack of notices.
 */
export function trimHead(text: string): string {
  if (text.length <= KEEP_CHARS) return text
  const cut = text.length - KEEP_CHARS
  const nl = text.indexOf('\n', cut)
  return `${HEAD_DROPPED}\n${text.slice(nl === -1 ? cut : nl + 1)}`
}

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

  const { state, received, finish, generation } = useLiveStream(streamUrl, {
    resetKey,
    onMessage: (e) => {
      try {
        const delta = JSON.parse(e.data) as string
        if (delta) setText((t) => trimHead(t + delta))
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
  const empty = !lines

  // Memoised for the scroll effect below, which must run when the footer's *content*
  // changes and not on every render.
  const footer = useMemo(
    () => logFooter({ end, errorMsg, state, received, empty }),
    [end, errorMsg, state, received, empty],
  )

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
              color: footer.kind === 'error' ? 'error.main' : 'text.secondary',
              opacity: 0.85,
            }}
          >
            {lines ? '\n' : ''}
            {footer.kind === 'busy' ? (
              <CircularProgress
                size={11}
                thickness={6}
                // Inherits the footer's muted colour so it reads as part of the note rather
                // than as a control, and sits on the text baseline beside it.
                sx={{ color: 'inherit', mr: 0.75, verticalAlign: 'middle' }}
              />
            ) : null}
            {footer.text}
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
