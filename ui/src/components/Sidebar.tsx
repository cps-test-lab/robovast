import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Collapse from '@mui/material/Collapse'
import Drawer from '@mui/material/Drawer'
import List from '@mui/material/List'
import ListItemButton from '@mui/material/ListItemButton'
import ListItemIcon from '@mui/material/ListItemIcon'
import ListItemText from '@mui/material/ListItemText'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import ExpandLess from '@mui/icons-material/ExpandLess'
import ExpandMore from '@mui/icons-material/ExpandMore'
import { robovast } from '@/lib/robovastClient'
import { formatCpuLabel, formatMemLabel } from '@/lib/format'

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
// a leaf; a topic with several views expands to show them nested beneath it (auto-expanded while it
// is the active topic). Branding sits at the top, the live connection/usage chip at the footer.
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
                {isActiveTopic ? <ExpandLess /> : <ExpandMore />}
              </ListItemButton>
              <Collapse in={isActiveTopic} unmountOnExit>
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
              </Collapse>
            </Box>
          )
        })}
      </List>

      <ConnectionStatus />
    </Drawer>
  )
}

// Passive service-connection + usage indicator, pinned to the sidebar footer (moved here from the
// old top AppBar). A dot goes green when the backend answers; cpu/mem stack on two lines to fit the
// narrow rail. The tooltip keeps version/backend discoverable.
function ConnectionStatus() {
  const usage = useQuery({
    queryKey: ['usage'],
    queryFn: () => robovast.resourceUsage(),
    refetchInterval: 15000,
    retry: false,
  })
  const version = useQuery({
    queryKey: ['version'],
    queryFn: () => robovast.version(),
    retry: false,
  })
  const connected = usage.isSuccess
  return (
    <Tooltip
      placement="right"
      title={
        version.isSuccess
          ? `robovast ${version.data.robovast_version}${version.data.backend ? ` · ${version.data.backend}` : ''}${
              connected ? ` · runs ${usage.data.parallel_runs ? 'in parallel' : 'sequentially'}` : ''
            }`
          : ''
      }
    >
      <Stack direction="row" spacing={1} alignItems="center" sx={{ px: 1 }}>
        <Box
          sx={{
            width: 8,
            height: 8,
            borderRadius: '50%',
            flexShrink: 0,
            bgcolor: connected ? 'success.main' : 'text.disabled',
            boxShadow: (t) => (connected ? `0 0 6px ${t.palette.success.main}` : 'none'),
          }}
        />
        {connected ? (
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="caption" color="text.secondary" display="block" lineHeight={1.35}>
              {formatCpuLabel(usage.data)}
            </Typography>
            <Typography variant="caption" color="text.secondary" display="block" lineHeight={1.35}>
              {formatMemLabel(usage.data)}
            </Typography>
          </Box>
        ) : (
          <Typography variant="caption" color="text.disabled">
            disconnected
          </Typography>
        )}
      </Stack>
    </Tooltip>
  )
}
