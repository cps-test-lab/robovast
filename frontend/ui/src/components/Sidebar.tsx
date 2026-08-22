import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Drawer from '@mui/material/Drawer'
import List from '@mui/material/List'
import ListItemButton from '@mui/material/ListItemButton'
import ListItemIcon from '@mui/material/ListItemIcon'
import ListItemText from '@mui/material/ListItemText'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { accent } from '@/colors'
import { robovast } from '@/lib/robovastClient'
import {
  bytesToGiB,
  formatBytes,
  formatBytesPair,
  formatCores,
  formatCpuUsed,
  formatMemUsed,
} from '@/lib/format'
import { BrandMark } from './BrandMark'
import { MeterBar } from './MeterBar'

export interface NavView {
  id: string
  label: string
  /** Optional, and rendered smaller than the topic icon above it — the indent alone is a weak
   *  signal on a narrow rail, and these icons are the same ones the campaign cards use to link
   *  into each view. */
  icon?: ReactNode
}

export interface NavTopic {
  id: string
  label: string
  icon: ReactNode
  /** When present with 2+ entries, the topic is an expandable parent of these views. */
  views?: NavView[]
}

export const SIDEBAR_WIDTH = 172

// The accent at low alpha — the one place outside the brand mark where it appears. A touch
// weaker than the teal this replaced ran at, because the accent is the lighter colour and reads
// brighter at equal alpha.
const selectedSx = {
  '&.Mui-selected, &.Mui-selected:hover': {
    backgroundColor: accent(0.12),
    border: `1px solid ${accent(0.28)}`,
  },
  borderRadius: 1,
  border: '1px solid transparent',
  px: 1,
}

// The whole app navigation: one permanent left rail with a tree menu. A topic with a single view is
// a leaf; a topic with several views renders them always nested beneath it (the active view is
// highlighted). Branding sits at the top, the live connection/usage chip at the footer.
export function Sidebar({
  topics,
  activeTopic,
  activeView,
  onSelect,
}: {
  topics: NavTopic[]
  activeTopic: string
  activeView: string
  onSelect: (topicId: string, viewId?: string) => void
}) {
  return (
    <Drawer
      variant="permanent"
      sx={{ width: SIDEBAR_WIDTH, flexShrink: 0 }}
      slotProps={{
        paper: {
          sx: {
            width: SIDEBAR_WIDTH,
            height: '100vh',
            display: 'flex',
            flexDirection: 'column',
            px: 0.75,
            py: 2,
            gap: 2,
          },
        },
      }}
    >
      {/* Wordmark in the text colour, mark in the accent: the accent then appears exactly twice
          in the rail — here and on the selected entry — so it still reads as "this one" rather
          than as decoration. */}
      <Stack direction="row" spacing={1} alignItems="center" sx={{ px: 1 }}>
        <BrandMark sx={{ color: 'primary.main', fontSize: 26 }} />
        <Typography variant="h6" sx={{ color: 'text.primary' }}>
          RoboVAST
        </Typography>
      </Stack>

      <List sx={{ flexGrow: 1, minHeight: 0, overflowY: 'auto', p: 0 }}>
        {topics.map((topic) => {
          const hasViews = !!topic.views && topic.views.length > 1
          const isActiveTopic = activeTopic === topic.id
          if (!hasViews) {
            return (
              <ListItemButton
                key={topic.id}
                selected={isActiveTopic}
                onClick={() => onSelect(topic.id)}
                sx={{ ...selectedSx, mb: 0.5 }}
              >
                <ListItemIcon sx={{ minWidth: 32, color: isActiveTopic ? 'primary.main' : 'text.secondary' }}>
                  {topic.icon}
                </ListItemIcon>
                <ListItemText primaryTypographyProps={{ fontWeight: 600 }}>{topic.label}</ListItemText>
              </ListItemButton>
            )
          }
          return (
            <Box key={topic.id} sx={{ mb: 0.5 }}>
              <ListItemButton
                onClick={() => onSelect(topic.id, topic.views![0].id)}
                sx={{ ...selectedSx }}
              >
                <ListItemIcon sx={{ minWidth: 32, color: isActiveTopic ? 'primary.main' : 'text.secondary' }}>
                  {topic.icon}
                </ListItemIcon>
                <ListItemText primaryTypographyProps={{ fontWeight: 600 }}>{topic.label}</ListItemText>
              </ListItemButton>
              <List disablePadding sx={{ mt: 0.5 }}>
                {topic.views!.map((view) => {
                  const isActiveView = isActiveTopic && activeView === view.id
                  return (
                    <ListItemButton
                      key={view.id}
                      selected={isActiveView}
                      onClick={() => onSelect(topic.id, view.id)}
                      sx={{ ...selectedSx, pl: 3, py: 0.5, mb: 0.25 }}
                    >
                      {view.icon ? (
                        <ListItemIcon
                          sx={{ minWidth: 26, color: isActiveView ? 'primary.main' : 'text.secondary' }}
                        >
                          {view.icon}
                        </ListItemIcon>
                      ) : null}
                      <ListItemText primaryTypographyProps={{ variant: 'body2' }}>
                        {view.label}
                      </ListItemText>
                    </ListItemButton>
                  )
                })}
              </List>
            </Box>
          )
        })}
      </List>

      <ConnectionStatus />
    </Drawer>
  )
}

// Passive service-connection + usage indicator, pinned to the sidebar footer (moved here from the
// old top AppBar). Stacked meters — cpu, mem, jobs while there is scenario work, and disk/store
// wherever the backend can report them — each labelled
// and captioned with its own hover tooltip; the whole block reads "disconnected" until the backend
// answers. Jobs is conditional because an always-present "0/0" on an empty track was indis-
// tinguishable from a dead widget, on a lane that is simply idle. Hidden now means nothing is
// running or queued; "disconnected" remains the only way to read a service that isn't answering.
function ConnectionStatus() {
  const usage = useQuery({
    queryKey: ['usage'],
    queryFn: () => robovast.resourceUsage(),
    refetchInterval: 15000,
    // This block is on every page and is the app's answer to "is the backend there?", so a
    // stale reading is worse here than anywhere: the poll pauses while the tab is hidden,
    // and 15s of a wrong meter — or a "disconnected" that healed while you were away — is
    // the first thing you look at on returning.
    refetchOnWindowFocus: true,
    retry: false,
  })
  if (!usage.isSuccess) {
    return (
      <Typography variant="caption" color="text.disabled" sx={{ px: 1 }}>
        disconnected
      </Typography>
    )
  }
  const u = usage.data
  const jobsTotal = u.jobs_running + u.jobs_pending
  return (
    <Stack spacing={0.5} sx={{ px: 1 }}>
      {jobsTotal > 0 ? (
        <UsageRow
          label="Jobs"
          tip={`${u.jobs_running} running, ${u.jobs_pending} pending`}
          fraction={u.jobs_running / jobsTotal}
          color="info.main"
          text={`${u.jobs_running}/${jobsTotal}`}
        />
      ) : null}
      <UsageRow
        label="CPU"
        tip={`${formatCores(u.cpu_used)} out of ${formatCores(u.cpu_capacity)} CPUs used`}
        fraction={u.cpu_capacity > 0 ? u.cpu_used / u.cpu_capacity : 0}
        text={formatCpuUsed(u)}
      />
      <UsageRow
        label="Mem"
        tip={`${bytesToGiB(u.memory_used_bytes).toFixed(0)} out of ${bytesToGiB(
          u.memory_capacity_bytes,
        ).toFixed(0)} GiB used`}
        fraction={u.memory_capacity_bytes > 0 ? u.memory_used_bytes / u.memory_capacity_bytes : 0}
        text={formatMemUsed(u)}
      />
      {/* Disk and Store are conditional for the reason the Jobs row is: null means "no
          verdict" — a service older than the fields, a backend whose filesystem could not
          be read, or a provider (a cloud bucket) with no capacity to report — and a 0/0
          track reads as either a full disk or a dead widget. Gated on the object, not on
          `backend`: the model is backend-neutral by contract, so the UI hides a row
          because the numbers are absent, never because it recognised a lane. No colour
          override: MeterBar turns red at 0.9, which is where kubelet's default hard
          eviction threshold (nodefs.available<10%) starts killing pods. */}
      {u.disk && u.disk.capacity_bytes > 0 ? (
        <UsageRow
          label="Disk"
          // NOT summed across the cluster: on kubernetes this is the filesystem of the node
          // the service runs on, which is the disk its workspaces are a hostPath on and so
          // the one that decides whether a campaign can be written. A twenty-node sum read
          // as tens of terabytes free while that one disk filled.
          tip={`${formatBytes(u.disk.used_bytes)} used, ${formatBytes(
            Math.max(0, u.disk.capacity_bytes - u.disk.used_bytes),
          )} free${u.backend === 'kubernetes' ? " on the service's node" : ''}`}
          fraction={u.disk.used_bytes / u.disk.capacity_bytes}
          text={formatBytesPair(u.disk.used_bytes, u.disk.capacity_bytes)}
        />
      ) : null}
      {u.store && u.store.capacity_bytes > 0 ? (
        <UsageRow
          label="Store"
          // The denominator is what this store can still REACH -- its own bytes plus what
          // the filesystem behind it will still take -- not that filesystem's size. A
          // provider whose store is an unbounded volume shares its disk with everything
          // else on that node, so its size was never a budget: it read 29 of 460 GiB on a
          // disk already 314 full. Named in the tip because it moves as the node fills,
          // which is the point rather than a wobble, and because it is NOT the Disk row's
          // denominator -- on a multi-node cluster this is one node while Disk is the sum.
          tip={`campaign results store: ${formatBytes(u.store.used_bytes)} used, ${formatBytes(
            Math.max(0, u.store.capacity_bytes - u.store.used_bytes),
          )} still free to it`}
          fraction={u.store.used_bytes / u.store.capacity_bytes}
          text={formatBytesPair(u.store.used_bytes, u.store.capacity_bytes)}
        />
      ) : null}
    </Stack>
  )
}

// One labelled meter row: a fixed-width caption ("Jobs"/"CPU"/"Mem"/"Disk"/"Store") beside a MeterBar
// whose in-track text is the compact "used/total". The whole row carries its own hover
// tooltip spelling the numbers out in words. `color` overrides the auto green→red fill
// (jobs use a fixed info tint since "full" isn't a warning there — for jobs "total" is
// the outstanding work, running+pending, not a capacity, so a full bar means the queue
// has drained; the tooltip is what splits the two numbers apart).
function UsageRow({
  label,
  tip,
  fraction,
  text,
  color,
}: {
  label: string
  tip: string
  fraction: number
  text: string
  color?: string
}) {
  return (
    <Tooltip placement="right" title={tip}>
      <Stack direction="row" spacing={0.75} alignItems="center">
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ width: 26, flexShrink: 0, lineHeight: 1 }}
        >
          {label}
        </Typography>
        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
          <MeterBar fraction={fraction} text={text} color={color} />
        </Box>
      </Stack>
    </Tooltip>
  )
}
