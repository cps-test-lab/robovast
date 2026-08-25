import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import MenuItem from '@mui/material/MenuItem'
import Stack from '@mui/material/Stack'
import Tab from '@mui/material/Tab'
import Tabs from '@mui/material/Tabs'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { robovast } from '@/lib/robovastClient'
import { configureVastSchema, isSchemaConfigured } from '@/lib/monaco'
import { type ConfigSource } from '@/lib/configSource'
import { useDialogs } from '@/components/DialogProvider'
import { ConfigEditorPane } from './ConfigEditorPane'
import { ConfigListPane } from './ConfigListPane'
import { ConfigViewPane } from './ConfigViewPane'
import { FilesView } from './FilesView'
import { useConfigEditor } from './useConfigEditor'

// The Config topic: one consolidated view. The left column is a tabbed [Config | Files] panel
// (full height), the right column keeps the resolved-config preview. The header bar and the
// .vast schema (loaded once, so Monaco gets completion + inline validation) live here; the editor
// and preview share a single selection via useConfigEditor.
//
// Two modes, one page. Normally it edits a workspace. With `campaignId` — a deep link from a
// campaign card, never a sidebar click — it shows that campaign's frozen `_config/` read-only: the
// configuration that campaign actually ran, which is not a workspace and is deliberately absent
// from the picker. The workspace state is left untouched while that is on screen, so leaving
// returns to whatever was being edited.
export function ConfigPage({
  campaignId = '',
  onExit,
}: {
  campaignId?: string
  onExit?: () => void
} = {}) {
  const qc = useQueryClient()
  const { prompt, confirm } = useDialogs()
  const [workspaceId, setWorkspaceId] = useState('')
  const [tab, setTab] = useState<'editor' | 'files'>('editor')
  const campaignMode = !!campaignId
  const source: ConfigSource = campaignMode
    ? { kind: 'campaign', id: campaignId }
    : { kind: 'workspace', id: workspaceId }
  const editor = useConfigEditor(source)

  const workspaces = useQuery({ queryKey: ['workspaces'], queryFn: () => robovast.listWorkspaces() })
  useQuery({
    queryKey: ['configSchema'],
    queryFn: async () => {
      const schema = await robovast.getConfigSchema()
      if (!isSchemaConfigured()) configureVastSchema(schema)
      return schema
    },
    staleTime: Infinity,
  })

  // Once workspaces load, default to the first if none is selected.
  useEffect(() => {
    const list = workspaces.data?.workspaces ?? []
    if (!workspaceId && list.length) setWorkspaceId(list[0].workspace_id)
  }, [workspaces.data, workspaceId])

  const createWs = useMutation({
    mutationFn: ({ name, fromCampaign = '' }: { name: string; fromCampaign?: string }) =>
      robovast.createWorkspace(name, fromCampaign),
    onSuccess: (ws) => {
      qc.invalidateQueries({ queryKey: ['workspaces'] })
      setWorkspaceId(ws.workspace_id)
      // Seeded from a campaign: the point was to get somewhere editable, so leave the read-only
      // view behind rather than leaving the new workspace selected underneath it.
      onExit?.()
    },
  })

  const newWorkspace = async () => {
    const name = await prompt({
      title: 'New workspace',
      label: 'Name',
      placeholder: 'my-project',
      confirmLabel: 'Create',
    })
    if (name === null) return
    createWs.mutate({ name })
  }

  const workspaceFromCampaign = async () => {
    const name = await prompt({
      title: 'New workspace from this campaign',
      label: 'Name',
      defaultValue: `${campaignId}-config`,
      confirmLabel: 'Create',
    })
    if (name === null) return
    createWs.mutate({ name, fromCampaign: campaignId })
  }

  const list = workspaces.data?.workspaces ?? []
  const selected = list.find((w) => w.workspace_id === workspaceId)

  const deleteWs = useMutation({
    mutationFn: (id: string) => robovast.deleteWorkspace(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workspaces'] })
      setWorkspaceId('')
    },
  })

  const removeWorkspace = async () => {
    if (!selected || selected.read_only) return
    const ok = await confirm({
      title: 'Delete workspace',
      message: (
        <>
          Delete workspace <strong>{selected.name || selected.workspace_id}</strong> and all its
          input files? Existing campaign results are unaffected. This cannot be undone.
        </>
      ),
      confirmLabel: 'Delete',
      danger: true,
    })
    if (!ok) return
    deleteWs.mutate(selected.workspace_id)
  }

  return (
    <Stack spacing={2} sx={{ height: 'calc(100vh - 48px)' }}>
      {createWs.isError && campaignMode ? (
        <Alert severity="error" variant="outlined">
          {(createWs.error as Error).message}
        </Alert>
      ) : null}
      {editor.filesError ? (
        <Alert severity="error" variant="outlined">
          {editor.filesError.message}
          {campaignMode
            ? ' — this campaign froze no configuration under _config/, which is what a campaign'
              + ' that failed before its configuration was staged looks like.'
            : null}
        </Alert>
      ) : null}
      {/* One header row in both modes. Where a workspace has a picker, a campaign has nothing
          to pick — so the same slot names the file on screen and says it is read-only, and the
          buttons beside it are the two ways out. */}
      <Stack direction="row" spacing={2} alignItems="center">
        <Typography variant="h6">Configuration</Typography>
        {campaignMode ? (
          <>
            <Chip
              size="small"
              variant="outlined"
              label={`${campaignId}/${editor.selected || '…'} (read-only)`}
              sx={{ fontFamily: 'monospace' }}
            />
            <Button size="small" onClick={workspaceFromCampaign} disabled={createWs.isPending}>
              Create workspace from this
            </Button>
            <Button size="small" onClick={onExit}>
              Back to workspaces
            </Button>
          </>
        ) : (
          <>
            <TextField
              select
              size="small"
              label="Workspace"
              // A workspace has to exist to be edited, so this is a picker over what the service has —
              // typing an id here would only produce a selection nothing can open.
              value={list.some((w) => w.workspace_id === workspaceId) ? workspaceId : ''}
              disabled={!list.length}
              onChange={(e) => setWorkspaceId(e.target.value)}
              sx={{ minWidth: 240 }}
            >
              {/* Gives the empty selection an option to match, so MUI does not warn about an
                  out-of-range value. Creating the first one is the button beside this field. */}
              <MenuItem value="" disabled>
                {list.length ? 'Select a workspace' : 'No workspaces yet'}
              </MenuItem>
              {list.map((w) => (
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
            <Button size="small" onClick={newWorkspace} disabled={createWs.isPending}>
              New workspace
            </Button>
            <Button
              size="small"
              color="error"
              onClick={removeWorkspace}
              disabled={!selected || selected.read_only || deleteWs.isPending}
              title={selected?.read_only ? 'Read-only workspaces cannot be deleted' : undefined}
            >
              Delete workspace
            </Button>
          </>
        )}
      </Stack>

      {!campaignMode && !workspaceId ? (
        <Alert severity="info" variant="outlined">
          Select or create a workspace, then author a <code>.vast</code> and upload your scenario/run files.
        </Alert>
      ) : (
        <Box
          sx={{
            display: 'grid',
            // Three columns: the editor, a narrow index of what it expands to, and the view the
            // .vast itself declares over the selected configuration. The middle one is deliberately
            // thin — it is a list of names — so the space goes to the two that show content.
            //
            // One column in campaign mode: Generate and validation are workspace operations, so
            // there is no resolved-config preview to put beside the editor.
            gridTemplateColumns: campaignMode ? '1fr' : '40fr 10fr 50fr',
            gap: 2,
            flexGrow: 1,
            minHeight: 0,
          }}
        >
          {/* Left column: a tabbed [Editor | Files] panel. Both panes stay mounted (toggled with
              display) so the editor buffer and the tree's expansion survive tab switches. */}
          <Box sx={{ display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0 }}>
            <Tabs
              value={tab}
              onChange={(_, v) => setTab(v)}
              sx={{ minHeight: 36, mb: 1, '& .MuiTab-root': { minHeight: 36, py: 0 } }}
            >
              <Tab value="editor" label="Config" />
              <Tab value="files" label="Files" />
            </Tabs>
            <Box sx={{ flexGrow: 1, minHeight: 0 }}>
              <Box sx={{ display: tab === 'editor' ? 'block' : 'none', height: '100%' }}>
                <ConfigEditorPane editor={editor} />
              </Box>
              <Box sx={{ display: tab === 'files' ? 'block' : 'none', height: '100%' }}>
                <FilesView source={source} />
              </Box>
            </Box>
          </Box>

          {/* Middle + right columns: what the .vast expands to, and what the .vast says to show
              about the selected one. Both stay visible regardless of the left tab. */}
          {campaignMode ? null : <ConfigListPane editor={editor} />}
          {campaignMode ? null : (
            <Box sx={{ minWidth: 0, minHeight: 0 }}>
              <ConfigViewPane editor={editor} workspaceId={workspaceId} />
            </Box>
          )}
        </Box>
      )}
    </Stack>
  )
}
