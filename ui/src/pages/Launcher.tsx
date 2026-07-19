import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import Chip from '@mui/material/Chip'
import FormControlLabel from '@mui/material/FormControlLabel'
import MenuItem from '@mui/material/MenuItem'
import Paper from '@mui/material/Paper'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import StopRoundedIcon from '@mui/icons-material/StopRounded'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { robovast, type Status } from '@/lib/robovastClient'
import { StatusView } from '@/components/StatusView'

const TERMINAL = ['finished', 'failed', 'stopped', 'error']
const isTerminal = (phase: string | undefined) => !!phase && TERMINAL.includes(phase)

// The browser analog of `vast exec cluster run`: a form over CreateCampaignRequest → create_campaign →
// campaign_id, then poll that campaign's live status (interaction lifted from robosito's CommandPlugin:
// launch, watch, stop). Backend is implicit in whichever service this UI is served by.
export function Launcher() {
  const qc = useQueryClient()
  const [workspaceId, setWorkspaceId] = useState('')
  const [configFilter, setConfigFilter] = useState('')
  const [runs, setRuns] = useState(1)
  const [postprocess, setPostprocess] = useState(true)
  const [uploadToShare, setUploadToShare] = useState(false)
  const [campaignId, setCampaignId] = useState<string | null>(null)
  const [configPath, setConfigPath] = useState('')

  const workspaces = useQuery({
    queryKey: ['workspaces'],
    queryFn: () => robovast.listWorkspaces(),
  })

  // The workspace's .vast files, to pick which one to run when there are several.
  const files = useQuery({
    queryKey: ['files', workspaceId],
    queryFn: () => robovast.listProjectFiles(workspaceId),
    enabled: !!workspaceId,
  })
  const vastFiles = (files.data?.files ?? [])
    .map((f) => f.path)
    .filter((p) => p.endsWith('.vast'))

  const create = useMutation({
    mutationFn: () =>
      robovast.createCampaign({
        workspace_id: workspaceId,
        config_path: configPath,
        config_filter: configFilter,
        runs,
        postprocess,
        upload_to_share: uploadToShare,
      }),
    onSuccess: (ref) => {
      setCampaignId(ref.campaign_id)
      qc.invalidateQueries({ queryKey: ['campaigns'] })
    },
  })

  const status = useQuery({
    queryKey: ['status', campaignId],
    queryFn: () => robovast.getStatus(campaignId!),
    enabled: !!campaignId,
    refetchInterval: (q) => (isTerminal((q.state.data as Status | undefined)?.phase) ? false : 1500),
  })

  const stop = useMutation({
    mutationFn: () => robovast.stop(campaignId!),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['status', campaignId] }),
  })

  const running = !!campaignId && !isTerminal(status.data?.phase)
  const canLaunch = !!workspaceId && !create.isPending

  return (
    <Stack spacing={2} sx={{ maxWidth: 640 }}>
      <Typography variant="h6">Launch campaign</Typography>

      <Paper sx={{ p: 2 }}>
        <Stack spacing={2}>
          <TextField
            select={!!workspaces.data?.workspaces.length}
            label="Workspace"
            value={workspaceId}
            onChange={(e) => {
              setWorkspaceId(e.target.value)
              setConfigPath('')
            }}
            helperText={
              workspaces.isError
                ? `could not list workspaces: ${(workspaces.error as Error).message}`
                : workspaces.data?.workspaces.length
                  ? 'editable project inputs to run'
                  : 'no workspaces found — enter an id (or empty for the CWD project)'
            }
            error={workspaces.isError}
            size="small"
            fullWidth
          >
            {(workspaces.data?.workspaces ?? []).map((w) => (
              <MenuItem key={w.workspace_id} value={w.workspace_id}>
                {w.name || w.workspace_id}
                {w.read_only ? (
                  <Chip
                    label="read-only"
                    size="small"
                    variant="outlined"
                    sx={{ ml: 1, height: 18, fontSize: '0.65rem' }}
                  />
                ) : null}
              </MenuItem>
            ))}
          </TextField>

          {vastFiles.length > 1 ? (
            <TextField
              select
              label="Config file (.vast)"
              value={configPath}
              onChange={(e) => setConfigPath(e.target.value)}
              helperText="this workspace has several .vast files — pick which to run"
              size="small"
              fullWidth
            >
              {vastFiles.map((p) => (
                <MenuItem key={p} value={p}>
                  {p}
                </MenuItem>
              ))}
            </TextField>
          ) : null}

          <TextField
            label="Config filter (glob, optional)"
            value={configFilter}
            onChange={(e) => setConfigFilter(e.target.value)}
            placeholder="run only matching configs"
            size="small"
            fullWidth
          />

          <Stack direction="row" spacing={2} alignItems="center">
            <TextField
              label="Runs per config"
              type="number"
              value={runs}
              onChange={(e) => setRuns(Math.max(1, Number(e.target.value) || 1))}
              size="small"
              sx={{ width: 160 }}
              slotProps={{ htmlInput: { min: 1 } }}
            />
            <FormControlLabel
              control={
                <Checkbox checked={postprocess} onChange={(e) => setPostprocess(e.target.checked)} />
              }
              label="Postprocess when done"
            />
            <FormControlLabel
              control={
                <Checkbox
                  checked={uploadToShare}
                  onChange={(e) => setUploadToShare(e.target.checked)}
                />
              }
              label="Upload to share when done"
            />
          </Stack>

          <Box>
            <Button
              variant="contained"
              startIcon={<PlayArrowRoundedIcon />}
              disabled={!canLaunch}
              onClick={() => create.mutate()}
            >
              Launch
            </Button>
          </Box>

          {create.isError ? (
            <Alert severity="error">Launch failed: {(create.error as Error).message}</Alert>
          ) : null}
        </Stack>
      </Paper>

      {campaignId ? (
        <Paper sx={{ p: 2 }}>
          <Stack direction="row" spacing={1} alignItems="center" mb={1.5}>
            <Typography variant="subtitle2" sx={{ fontFamily: 'monospace' }}>
              {campaignId}
            </Typography>
            <Box flexGrow={1} />
            {running ? (
              <Button
                size="small"
                variant="outlined"
                color="error"
                startIcon={<StopRoundedIcon />}
                disabled={stop.isPending}
                onClick={() => stop.mutate()}
              >
                Stop
              </Button>
            ) : null}
          </Stack>
          {status.data ? (
            <StatusView status={status.data} hideLog />
          ) : status.isError ? (
            <Typography variant="caption" color="text.secondary">
              waiting for status… ({(status.error as Error).message})
            </Typography>
          ) : (
            <Typography variant="caption" color="text.secondary">
              starting…
            </Typography>
          )}
        </Paper>
      ) : null}
    </Stack>
  )
}
