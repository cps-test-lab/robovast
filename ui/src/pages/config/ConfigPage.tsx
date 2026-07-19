import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import MenuItem from '@mui/material/MenuItem'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { robovast } from '@/lib/robovastClient'
import { configureVastSchema, isSchemaConfigured } from '@/lib/monaco'
import { useDialogs } from '@/components/DialogProvider'
import { KeepAlive } from '@/components/KeepAlive'
import { ConfigView } from './ConfigView'
import { FilesView } from './FilesView'

// The Config topic container: owns the selected workspace and the shared workspace bar, then renders
// the active subview (Configuration or Files). The .vast schema is loaded once here so Monaco in
// either subview gets completion + inline validation.
export function ConfigPage({ view }: { view: string }) {
  const qc = useQueryClient()
  const { prompt } = useDialogs()
  const [workspaceId, setWorkspaceId] = useState('')

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
    mutationFn: (name: string) => robovast.createWorkspace(name),
    onSuccess: (ws) => {
      qc.invalidateQueries({ queryKey: ['workspaces'] })
      setWorkspaceId(ws.workspace_id)
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
    createWs.mutate(name)
  }

  return (
    <Stack spacing={2}>
      <Stack direction="row" spacing={2} alignItems="center">
        <Typography variant="h6">{view === 'files' ? 'Workspace files' : 'Configuration'}</Typography>
        <TextField
          select={!!workspaces.data?.workspaces.length}
          size="small"
          label="Workspace"
          value={workspaceId}
          onChange={(e) => setWorkspaceId(e.target.value)}
          sx={{ minWidth: 240 }}
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
        <Button size="small" onClick={newWorkspace} disabled={createWs.isPending}>
          New workspace
        </Button>
      </Stack>

      {!workspaceId ? (
        <Alert severity="info" variant="outlined">
          Select or create a workspace, then author a <code>.vast</code> and upload your scenario/run files.
        </Alert>
      ) : (
        // Both subviews are kept alive so each retains its state (selected .vast/file, editor buffer,
        // preview) when the user switches between Configuration and Files.
        <Box>
          <KeepAlive active={view !== 'files'}>
            <ConfigView workspaceId={workspaceId} />
          </KeepAlive>
          <KeepAlive active={view === 'files'}>
            <FilesView workspaceId={workspaceId} />
          </KeepAlive>
        </Box>
      )}
    </Stack>
  )
}
