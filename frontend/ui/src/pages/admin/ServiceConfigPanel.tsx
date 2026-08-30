import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { robovast, type ServiceSetting } from '@/lib/robovastClient'
import { groupsOf, WITHHELD_REASON } from './serviceConfig'

/** One setting: its name, then whatever we are able to say about its value. */
function SettingRow({ setting }: { setting: ServiceSetting }) {
  const reason = setting.withheld ? WITHHELD_REASON[setting.withheld] : null
  return (
    <Stack direction="row" spacing={1} alignItems="baseline">
      <Tooltip title={setting.description || ''} placement="left">
        <Typography
          variant="caption"
          sx={{ fontFamily: 'monospace', width: 300, flexShrink: 0, wordBreak: 'break-all' }}
        >
          {setting.key}
        </Typography>
      </Tooltip>
      {setting.value !== null && setting.value !== undefined ? (
        <Typography
          variant="caption"
          sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}
        >
          {setting.value}
        </Typography>
      ) : setting.is_set ? (
        // Set, but not shown. The chip says the fact that matters -- it IS configured --
        // and the tooltip says why the value is not beside it.
        <Tooltip title={reason ?? ''}>
          <Chip label="set" size="small" variant="outlined" sx={{ height: 18 }} />
        </Tooltip>
      ) : (
        <Typography variant="caption" color="text.disabled">
          not set
          {/* A default is only worth printing where the setting is unset: it is what the
              service is doing INSTEAD, which is the question an empty row raises. */}
          {setting.default ? ` — default: ${setting.default}` : ''}
        </Typography>
      )}
    </Stack>
  )
}

/**
 * What this service is running with, read back out of its own environment.
 *
 * Read-only. When settings become editable this is where an edit mode attaches: the rows
 * already carry everything an input would need, and the response gains a field naming
 * which of them accept a write.
 */
export function ServiceConfigPanel() {
  const config = useQuery({
    queryKey: ['serviceConfig'],
    queryFn: robovast.serviceConfig,
    // No refetch interval: this changes when the pod restarts and at no other time, so a
    // poll would ask a question whose answer cannot have moved.
    staleTime: Infinity,
    retry: false,
  })

  if (!config.isSuccess) {
    return (
      <Typography variant="caption" color="text.disabled">
        {config.isError ? 'could not read the configuration' : 'loading…'}
      </Typography>
    )
  }

  return (
    <Stack spacing={2}>
      {groupsOf(config.data.settings).map(([group, rows]) => (
        <Box key={group}>
          <Typography variant="subtitle2" gutterBottom>
            {group}
          </Typography>
          <Stack spacing={0.25}>
            {rows.map((s) => <SettingRow key={s.key} setting={s} />)}
          </Stack>
        </Box>
      ))}
      {/* Last, because it is what to do about anything above -- and it is the service's
          answer, not the UI's: only the service knows whether a change here needs a pod
          rolled or a local process restarted. */}
      <Typography variant="caption" color="text.secondary">
        {config.data.how_to_change}
      </Typography>
    </Stack>
  )
}
