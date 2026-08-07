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
import { robovast } from '@/lib/robovastClient'
import {
  bytesToGiB,
  formatCores,
  formatCpuUsed,
  formatMemUsed,
} from '@/lib/format'
import { MeterBar } from './MeterBar'

export interface NavView {
  id: string
  label: string
}

export interface NavTopic {
  id: string
  label: string
  icon: ReactNode
  /** When present with 2+ entries, the topic is an expandable parent of these views. */
  views?: NavView[]
}

export const SIDEBAR_WIDTH = 172

const selectedSx = {
  '&.Mui-selected, &.Mui-selected:hover': {
    backgroundColor: 'rgba(45, 212, 191, 0.14)',
    border: '1px solid rgba(45, 212, 191, 0.28)',
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
      <Typography variant="h6" sx={{ color: 'primary.main', px: 1 }}>
        RoboVAST
      </Typography>

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
                {topic.views!.map((view) => (
                  <ListItemButton
                    key={view.id}
                    selected={isActiveTopic && activeView === view.id}
                    onClick={() => onSelect(topic.id, view.id)}
                    sx={{ ...selectedSx, pl: 3, py: 0.5, mb: 0.25 }}
                  >
                    <ListItemText primaryTypographyProps={{ variant: 'body2' }}>
                      {view.label}
                    </ListItemText>
                  </ListItemButton>
                ))}
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
// old top AppBar). Stacked meters — cpu, mem, and jobs while there is scenario work — each labelled
// and captioned with its own hover tooltip; the whole block reads "disconnected" until the backend
// answers. Jobs is conditional because an always-present "0/0" on an empty track was indis-
// tinguishable from a dead widget, on a lane that is simply idle. Hidden now means nothing is
// running or queued; "disconnected" remains the only way to read a service that isn't answering.
function ConnectionStatus() {
  const usage = useQuery({
    queryKey: ['usage'],
    queryFn: () => robovast.resourceUsage(),
    refetchInterval: 15000,
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
    </Stack>
  )
}

// One labelled meter row: a fixed-width caption ("Jobs"/"CPU"/"Mem") beside a MeterBar
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
