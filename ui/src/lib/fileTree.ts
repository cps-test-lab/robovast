// Client-side helpers for the workspace Files view: turn the service's flat list of file paths into
// a tree, and hide reserved artefacts. The service returns a flat FileMeta[] (path split on '/'), so
// the tree is derived here rather than server-side.

/** A reserved path is anything with a dot-prefixed segment: campaign cache (`.cache`), robovast
 *  internals (`.robovast*`, `.robovast_plugins/`), VCS (`.git`), and other hidden dot-files. These
 *  are never project inputs the user edits, so the Files tree omits them. */
export function isReserved(path: string): boolean {
  return path.split('/').some((seg) => seg.startsWith('.'))
}

export interface TreeFile {
  kind: 'file'
  name: string
  /** Full workspace-relative path (what the file APIs expect). */
  path: string
}

export interface TreeDir {
  kind: 'dir'
  name: string
  /** Full path of the directory (joined segments); '' for the root. */
  path: string
  children: TreeNode[]
}

export type TreeNode = TreeFile | TreeDir

/** Build a sorted tree (dirs first, then files, each alphabetical) from flat paths, skipping
 *  reserved paths. Intermediate directories are synthesized from the path segments. */
export function buildTree(paths: string[]): TreeNode[] {
  const root: TreeDir = { kind: 'dir', name: '', path: '', children: [] }

  for (const path of paths) {
    if (isReserved(path)) continue
    const segments = path.split('/').filter(Boolean)
    let cursor = root
    segments.forEach((seg, i) => {
      const isLeaf = i === segments.length - 1
      if (isLeaf) {
        cursor.children.push({ kind: 'file', name: seg, path })
        return
      }
      const dirPath = segments.slice(0, i + 1).join('/')
      let next = cursor.children.find(
        (c): c is TreeDir => c.kind === 'dir' && c.name === seg,
      )
      if (!next) {
        next = { kind: 'dir', name: seg, path: dirPath, children: [] }
        cursor.children.push(next)
      }
      cursor = next
    })
  }

  const sortNodes = (nodes: TreeNode[]): TreeNode[] => {
    nodes.sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === 'dir' ? -1 : 1
      return a.name.localeCompare(b.name)
    })
    for (const n of nodes) if (n.kind === 'dir') sortNodes(n.children)
    return nodes
  }
  return sortNodes(root.children)
}
