import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ResultNote } from './ResultNote'

describe('ResultNote', () => {
  it('renders nothing for an answer the service says nothing about', () => {
    expect(renderToStaticMarkup(<ResultNote note={null} />)).toBe('')
    expect(renderToStaticMarkup(<ResultNote />)).toBe('')
  })

  it('shows the note beside the rows, not only on hover', () => {
    const note = 'the answer leaves out what could not be built or decoded: rosbag2_torn '
      + 'incomplete for c/cfg/0: topic /torn (std_msgs/msg/String) is undecodable'
    const html = renderToStaticMarkup(<ResultNote note={note} />)
    expect(html).toContain('role="alert"')
    expect(html).toContain('rosbag2_torn incomplete for c/cfg/0')
  })
})
