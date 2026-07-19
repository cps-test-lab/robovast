import { forwardRef, type Ref, type HTMLAttributes } from 'react'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import { useTreeItem2, type UseTreeItem2Parameters } from '@mui/x-tree-view/useTreeItem2'
import {
  TreeItem2Content,
  TreeItem2GroupTransition,
  TreeItem2IconContainer,
  TreeItem2Label,
  TreeItem2Root,
} from '@mui/x-tree-view/TreeItem2'
import { TreeItem2Icon } from '@mui/x-tree-view/TreeItem2Icon'
import { TreeItem2Provider } from '@mui/x-tree-view/TreeItem2Provider'
import { statusColor, type ResultsTreeItem } from '@/lib/resultsTree'

// Custom RichTreeView item for the Results Explorer: the default expand/collapse + label, with a
// colored pass/fail status dot (same idiom as the sidebar connection dot) and a trailing
// passed/total count chip. Per-item metadata (status, count) is read back from the tree via
// `publicAPI.getItem(itemId)` — RichTreeView only threads {id,label} to the slot itself.
interface StatusTreeItemProps
  extends Omit<UseTreeItem2Parameters, 'rootRef'>,
    Omit<HTMLAttributes<HTMLLIElement>, 'onFocus'> {}

export const StatusTreeItem = forwardRef(function StatusTreeItem(
  props: StatusTreeItemProps,
  ref: Ref<HTMLLIElement>,
) {
  const { id, itemId, label, disabled, children, ...other } = props
  const {
    getRootProps,
    getContentProps,
    getIconContainerProps,
    getLabelProps,
    getGroupTransitionProps,
    status,
    publicAPI,
  } = useTreeItem2({ id, itemId, children, label, disabled, rootRef: ref })

  const item = publicAPI.getItem(itemId) as ResultsTreeItem | undefined

  return (
    <TreeItem2Provider itemId={itemId}>
      <TreeItem2Root {...getRootProps(other)}>
        <TreeItem2Content {...getContentProps()}>
          <TreeItem2IconContainer {...getIconContainerProps()}>
            <TreeItem2Icon status={status} />
          </TreeItem2IconContainer>
          {item && item.kind !== 'placeholder' ? (
            <Box
              sx={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                flexShrink: 0,
                mr: 1,
                bgcolor: statusColor(item.status),
              }}
            />
          ) : null}
          <TreeItem2Label
            {...getLabelProps()}
            sx={{
              flexGrow: 1,
              minWidth: 0,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              fontStyle: item?.kind === 'placeholder' ? 'italic' : undefined,
              color: item?.kind === 'placeholder' ? 'text.disabled' : undefined,
            }}
          />
          {item?.count ? (
            <Chip
              label={item.count}
              size="small"
              variant="outlined"
              sx={{ ml: 1, height: 18, fontSize: '0.65rem', flexShrink: 0 }}
            />
          ) : null}
        </TreeItem2Content>
        {children ? <TreeItem2GroupTransition {...getGroupTransitionProps()} /> : null}
      </TreeItem2Root>
    </TreeItem2Provider>
  )
})
