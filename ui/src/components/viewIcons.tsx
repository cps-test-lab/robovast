import AccountTreeRoundedIcon from '@mui/icons-material/AccountTreeRounded'
import PlayCircleOutlineRoundedIcon from '@mui/icons-material/PlayCircleOutlineRounded'
import StorageRoundedIcon from '@mui/icons-material/StorageRounded'

// The icons of the three Results sub-views, in one place because they are used twice: on the
// sidebar entries and on the campaign-card shortcut buttons that jump straight into a view. Two
// call sites picking their own icon for the same destination is how a shortcut stops looking like
// the thing it opens.
//
// Each names what its view *is* — the campaign → config → run tree, a run being replayed, the
// database the browser queries — and none repeats a topic icon (Tune / RocketLaunch / Insights).
export const ExplorerIcon = AccountTreeRoundedIcon
export const RunViewIcon = PlayCircleOutlineRoundedIcon
export const DataBrowserIcon = StorageRoundedIcon

/** Sub-view icons sit one step below the 24px (MUI default) topic icons, so the sidebar's two
 *  levels stay distinguishable at a glance even where the indent is easy to miss. */
export const SUBVIEW_ICON_SIZE = 18
