import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import Chip from '@mui/material/Chip'
import Collapse from '@mui/material/Collapse'
import FormControlLabel from '@mui/material/FormControlLabel'
import MenuItem from '@mui/material/MenuItem'
import Paper from '@mui/material/Paper'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import TuneRoundedIcon from '@mui/icons-material/TuneRounded'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import { robovast } from '@/lib/robovastClient'

// Pull the `execution.runs` scalar out of a .vast (YAML) so the launcher can prefill "Runs per config"
// with whatever the file declares. We scan for the top-level `execution:` block and read the integer
// `runs:` directly under it. Returns null when runs is absent or a non-literal (e.g. `runs: runs`
// referencing a variable), in which case the caller keeps the current value.
function runsFromVast(content: string): number | null {
  const lines = content.split(/\r?\n/)
  let inExecution = false
  for (const line of lines) {
    const m = line.match(/^(\s*)([\w-]+):(.*)$/)
    if (!m) continue
    const [, indent, key, rest] = m
    if (indent.length === 0) {
      // A new top-level key starts (or ends) the execution block.
      inExecution = key === 'execution'
      continue
    }
    if (inExecution && key === 'runs') {
      const n = Number(rest.trim())
      return Number.isInteger(n) && n > 0 ? n : null
    }
  }
  return null
}

// The browser analog of `vast exec cluster run`: a compact form over CreateCampaignRequest →
// create_campaign, sitting at the top of the Campaigns page. On success it only invalidates the
// ['campaigns'] list — the launched campaign then shows up as a card below like any other, so there
// is no second, page-local copy of campaign state to drift out of sync with a delete.
export function LaunchBar() {
  const qc = useQueryClient()
  const [workspaceId, setWorkspaceId] = useState('')
  const [configFilter, setConfigFilter] = useState('')
  const [campaignName, setCampaignName] = useState('')
  const [runs, setRuns] = useState(1)
  const [postprocess, setPostprocess] = useState(true)
  const [uploadToShare, setUploadToShare] = useState(true)
  const [configPath, setConfigPath] = useState('')
  const [backend, setBackend] = useState('')
  const [showOptions, setShowOptions] = useState(false)

  const workspaces = useQuery({
    queryKey: ['workspaces'],
    queryFn: () => robovast.listWorkspaces(),
  })

  // The lanes this service offers. A backend picker is shown only when there is
  // more than one (a `vast serve --backend local+cluster`); it defaults to
  // cluster (the scaled lane) and is omitted from the request otherwise, so a
  // single-backend service is unaffected.
  const version = useQuery({ queryKey: ['version'], queryFn: () => robovast.version() })
  const backends = version.data?.backends ?? []
  const multiBackend = backends.length > 1
  useEffect(() => {
    if (!multiBackend || backend) return
    setBackend(backends.includes('cluster') ? 'cluster' : backends[0])
  }, [multiBackend, backends, backend])

  // On startup pick a workspace so the form is ready to launch: the most recently
  // created one if creation times are known, otherwise the first listed.
  useEffect(() => {
    if (workspaceId) return
    const list = workspaces.data?.workspaces
    if (!list?.length) return
    const latest = [...list].sort(
      (a, b) => (Date.parse(b.created_at ?? '') || 0) - (Date.parse(a.created_at ?? '') || 0),
    )[0]
    setWorkspaceId(latest.workspace_id)
  }, [workspaces.data, workspaceId])

  // The workspace's .vast files, to pick which one to run when there are several.
  const files = useQuery({
    queryKey: ['files', workspaceId],
    queryFn: () => robovast.listProjectFiles(workspaceId),
    enabled: !!workspaceId,
  })
  const vastFiles = (files.data?.files ?? [])
    .map((f) => f.path)
    .filter((p) => p.endsWith('.vast'))

  // Preselect the first .vast file if the user hasn't chosen one yet.
  useEffect(() => {
    if (configPath || !vastFiles.length) return
    setConfigPath(vastFiles[0])
  }, [configPath, vastFiles])

  // Read the selected .vast so we can prefill "Runs per config" from its execution.runs.
  const configFile = useQuery({
    queryKey: ['file', workspaceId, configPath],
    queryFn: () => robovast.readProjectFile(workspaceId, configPath),
    enabled: !!workspaceId && !!configPath,
  })

  // When the selected .vast changes (or its content is edited), adopt its declared runs count. Keyed
  // on the content string, so a later manual edit to the Runs field is not clobbered by re-renders.
  useEffect(() => {
    const content = configFile.data?.content
    if (!content) return
    const declared = runsFromVast(content)
    if (declared != null) setRuns(declared)
  }, [configFile.data?.content])

  const create = useMutation({
    mutationFn: () =>
      robovast.createCampaign({
        workspace_id: workspaceId,
        config_path: configPath,
        config_filter: configFilter,
        campaign_name: campaignName.trim(),
        runs,
        postprocess,
        upload_to_share: uploadToShare,
        backend: multiBackend ? backend : undefined,
      }),
    // The launched campaign becomes a card in the list below; nothing else to hold onto here.
    onSuccess: () => qc.invalidateQueries({ queryKey: ['campaigns'] }),
  })

  const canLaunch = !!workspaceId && !create.isPending

  return (
    <Paper sx={{ p: 2 }}>
      <Stack spacing={1.5}>
        <Stack
          direction="row"
          spacing={2}
          alignItems="flex-end"
          sx={{ flexWrap: 'wrap', rowGap: 1.5 }}
        >
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
                  ? undefined
                  : 'no workspaces found — enter an id (or empty for the CWD project)'
            }
            error={workspaces.isError}
            size="small"
            sx={{ minWidth: 200 }}
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
              size="small"
              sx={{ minWidth: 200 }}
            >
              {vastFiles.map((p) => (
                <MenuItem key={p} value={p}>
                  {p}
                </MenuItem>
              ))}
            </TextField>
          ) : null}

          {multiBackend ? (
            <TextField
              select
              label="Backend"
              value={backend}
              onChange={(e) => setBackend(e.target.value)}
              size="small"
              sx={{ minWidth: 130 }}
              helperText={backend === 'local' ? 'pilot (Docker)' : 'scaled (cluster)'}
            >
              {backends.map((b) => (
                <MenuItem key={b} value={b}>
                  {b}
                </MenuItem>
              ))}
            </TextField>
          ) : null}

          <Button
            variant="contained"
            startIcon={<PlayArrowRoundedIcon />}
            disabled={!canLaunch}
            onClick={() => create.mutate()}
          >
            Launch
          </Button>

          <Box flexGrow={1} />

          <Button
            size="small"
            color="inherit"
            startIcon={<TuneRoundedIcon />}
            onClick={() => setShowOptions((v) => !v)}
          >
            Options
          </Button>
        </Stack>

        <Collapse in={showOptions} unmountOnExit>
          <Stack
            direction="row"
            spacing={2}
            alignItems="center"
            sx={{ flexWrap: 'wrap', rowGap: 1 }}
          >
            <TextField
              label="Campaign name override (optional)"
              value={campaignName}
              onChange={(e) => setCampaignName(e.target.value)}
              placeholder="overrides the .vast name"
              size="small"
              sx={{ minWidth: 260 }}
              slotProps={{ inputLabel: { shrink: true } }}
            />
            <TextField
              label="Runs per config"
              type="number"
              value={runs}
              onChange={(e) => setRuns(Math.max(1, Number(e.target.value) || 1))}
              size="small"
              sx={{ width: 140 }}
              slotProps={{ htmlInput: { min: 1 } }}
            />
            <TextField
              label="Config filter (glob, optional)"
              value={configFilter}
              onChange={(e) => setConfigFilter(e.target.value)}
              placeholder="run only matching configs"
              size="small"
              sx={{ minWidth: 240 }}
              slotProps={{ inputLabel: { shrink: true } }}
            />
            <FormControlLabel
              control={
                <Checkbox checked={postprocess} onChange={(e) => setPostprocess(e.target.checked)} />
              }
              label="Postprocess"
            />
            <FormControlLabel
              control={
                <Checkbox
                  checked={uploadToShare}
                  onChange={(e) => setUploadToShare(e.target.checked)}
                />
              }
              label="Upload to share"
            />
          </Stack>
        </Collapse>

        {create.isError ? (
          <Alert severity="error">Launch failed: {(create.error as Error).message}</Alert>
        ) : null}
      </Stack>
    </Paper>
  )
}
