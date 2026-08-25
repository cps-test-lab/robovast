import AccountTreeRoundedIcon from '@mui/icons-material/AccountTreeRounded'
import EditNoteRoundedIcon from '@mui/icons-material/EditNoteRounded'
import PlayCircleOutlineRoundedIcon from '@mui/icons-material/PlayCircleOutlineRounded'
import StorageRoundedIcon from '@mui/icons-material/StorageRounded'

// The icons of every view a campaign card can jump into, in one place because they are used
// twice: on the sidebar entries and on the campaign-card shortcut buttons. Two call sites picking
// their own icon for the same destination is how a shortcut stops looking like the thing it opens.
//
// Each names what its view *is* — the campaign → config → run tree, a run being replayed, the
// database the browser queries — and none repeats a topic icon (RocketLaunch / Insights).
export const ExplorerIcon = AccountTreeRoundedIcon
export const RunViewIcon = PlayCircleOutlineRoundedIcon
export const DataBrowserIcon = StorageRoundedIcon

/** The Config topic — a page with a pen on it. Unlike the three above this *is* a topic icon
 *  (Config is a leaf topic), shared here because a campaign card links into it too. */
export const ConfigIcon = EditNoteRoundedIcon

/** Sub-view icons sit one step below the 24px (MUI default) topic icons, so the sidebar's two
 *  levels stay distinguishable at a glance even where the indent is easy to miss. */
export const SUBVIEW_ICON_SIZE = 18
