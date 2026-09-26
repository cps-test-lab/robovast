import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Autocomplete from '@mui/material/Autocomplete'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import CircularProgress from '@mui/material/CircularProgress'
import Collapse from '@mui/material/Collapse'
import FormControlLabel from '@mui/material/FormControlLabel'
import MenuItem from '@mui/material/MenuItem'
import Paper from '@mui/material/Paper'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import TuneRoundedIcon from '@mui/icons-material/TuneRounded'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import { useActiveView } from '@/lib/activeView'
import { isGlob, matchConfigs, matchesPattern } from '@/lib/configFilter'
import { DESCRIPTION_MAX_LEN, robovast } from '@/lib/robovastClient'
import { ErrorText } from '@/components/StatusView'

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

// The browser analog of `vast workspace run`: a compact form over CreateCampaignRequest →
// create_campaign, sitting at the top of the Campaigns page. On success it only invalidates the
// ['campaigns'] list — the launched campaign then shows up as a card below like any other, so there
// is no second, page-local copy of campaign state to drift out of sync with a delete.
export function LaunchBar() {
  const qc = useQueryClient()
  const [workspaceId, setWorkspaceId] = useState('')
  // The config filter as chips (picked names, or globs entered with Enter) plus whatever is still
  // being typed; together they are the comma-separated filter the service reads.
  const [filterTokens, setFilterTokens] = useState<string[]>([])
  const [filterInput, setFilterInput] = useState('')
  const [campaignName, setCampaignName] = useState('')
  const [description, setDescription] = useState('')
  const [runs, setRuns] = useState(1)
  const [postprocess, setPostprocess] = useState(true)
  // Off by default: uploading streams the campaign to an external share, which is a
  // deliberate act of publication rather than a step of running one.
  const [uploadToShare, setUploadToShare] = useState(false)
  const [configPath, setConfigPath] = useState('')
  const [showOptions, setShowOptions] = useState(false)

  // What this form can launch is read on arrival rather than once per session: a workspace
  // created, a `.vast` added or edited elsewhere — in Config, from the CLI, by another agent —
  // must be offered here without a reload. Cheap reads, and nothing here holds an edit of its own
  // that a refresh could discard. See lib/activeView.tsx.
  const active = useActiveView()
  const workspaces = useQuery({
    queryKey: ['workspaces'],
    queryFn: () => robovast.listWorkspaces(),
    enabled: active,
  })

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
    enabled: active && !!workspaceId,
  })
  const vastFiles = (files.data?.entries ?? []).filter((p) => p.endsWith('.vast'))

  // Preselect the first .vast file if the user hasn't chosen one yet.
  useEffect(() => {
    if (configPath || !vastFiles.length) return
    setConfigPath(vastFiles[0])
  }, [configPath, vastFiles])

  // Read the selected .vast so we can prefill "Runs per config" from its execution.runs.
  const configFile = useQuery({
    queryKey: ['file', workspaceId, configPath],
    queryFn: () => robovast.readProjectFile(workspaceId, configPath),
    enabled: active && !!workspaceId && !!configPath,
  })

  // When the selected .vast changes (or its content is edited), adopt its declared runs count. Keyed
  // on the content string, so a later manual edit to the Runs field is not clobbered by re-renders.
  useEffect(() => {
    const content = configFile.data?.content
    if (!content) return
    const declared = runsFromVast(content)
    if (declared != null) setRuns(declared)
  }, [configFile.data?.content])

  // The names the selected .vast expands to, for the filter's dropdown. Composing can take long, so
  // the service does it in the background and this polls until it lands. Keyed on the file's
  // content, so an edit made elsewhere asks again.
  const configNames = useQuery({
    queryKey: ['configNames', workspaceId, configPath, configFile.data?.content],
    queryFn: () => robovast.listConfigNames(workspaceId, configPath),
    enabled: active && !!workspaceId && !!configPath && configFile.isSuccess,
    refetchInterval: (q) => (q.state.data?.state === 'composing' ? 1000 : false),
  })
  const names = configNames.data?.state === 'ready' ? configNames.data.names : []
  const composing = configNames.isLoading || configNames.data?.state === 'composing'

  // A filter picked for one .vast means nothing for the next.
  useEffect(() => {
    setFilterTokens([])
    setFilterInput('')
  }, [workspaceId, configPath])

  const configFilter = [...filterTokens, filterInput.trim()].filter(Boolean).join(',')
  const matched = configNames.data?.state === 'ready' ? matchConfigs(names, configFilter) : null
  // Launching this would only produce a campaign that fails composing its configurations.
  const noMatch = !!configFilter && matched?.length === 0

  const progress = configNames.data?.progress
  const filterHelp = configNames.isError
    ? (configNames.error as Error).message
    : configNames.data?.state === 'failed'
      ? `could not list configurations: ${configNames.data.error.split('\n')[0]}`
      : composing
        ? progress
          ? `composing: ${progress.done} of ${progress.total} variations done`
          : 'composing configurations…'
        : matched
          ? noMatch
            ? `matches none of the ${names.length} configurations`
            : configFilter
              ? `${matched.length} of ${names.length} configurations`
              : `${names.length} configurations`
          : undefined

  const create = useMutation({
    mutationFn: () =>
      robovast.createCampaign({
        workspace_id: workspaceId,
        config_path: configPath,
        config_filter: configFilter,
        campaign_name: campaignName.trim(),
        description: description.trim(),
        runs,
        postprocess,
        upload_to_share: uploadToShare,
      }),
    // The launched campaign becomes a card in the list below; nothing else to hold onto here.
    onSuccess: () => qc.invalidateQueries({ queryKey: ['campaigns'] }),
  })

  const canLaunch = !!workspaceId && !create.isPending && !noMatch

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
              label="Description (optional)"
              value={description}
              onChange={(e) => setDescription(e.target.value.slice(0, DESCRIPTION_MAX_LEN))}
              placeholder="what this run is for"
              size="small"
              sx={{ minWidth: 320 }}
              slotProps={{
                inputLabel: { shrink: true },
                htmlInput: { maxLength: DESCRIPTION_MAX_LEN },
              }}
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
            <Autocomplete
              multiple
              freeSolo
              size="small"
              sx={{ minWidth: 320 }}
              options={names}
              value={filterTokens}
              onChange={(_, v) => setFilterTokens(v)}
              inputValue={filterInput}
              onInputChange={(_, v) => setFilterInput(v)}
              // A glob narrows the list to what it selects; plain text to the names containing it.
              filterOptions={(options, { inputValue }) => {
                const q = inputValue.trim()
                if (!q) return options
                return options.filter((n) => (isGlob(q) ? matchesPattern(n, q) : n.includes(q)))
              }}
              renderInput={(params) => (
                <TextField
                  {...params}
                  label="Config filter (optional)"
                  placeholder={filterTokens.length ? undefined : 'names or globs, e.g. config1-*'}
                  helperText={filterHelp}
                  error={noMatch || configNames.isError || configNames.data?.state === 'failed'}
                  InputLabelProps={{ ...params.InputLabelProps, shrink: true }}
                  InputProps={{
                    ...params.InputProps,
                    endAdornment: (
                      <>
                        {composing ? <CircularProgress size={16} /> : null}
                        {params.InputProps.endAdornment}
                      </>
                    ),
                  }}
                />
              )}
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

        {noMatch && !showOptions ? (
          <Alert severity="warning">
            The config filter matches none of the {names.length} configurations.
          </Alert>
        ) : null}

        {create.isError ? (
          <Alert severity="error">
            Launch failed.
            <ErrorText>{(create.error as Error).message}</ErrorText>
          </Alert>
        ) : null}
      </Stack>
    </Paper>
  )
}
