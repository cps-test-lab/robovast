// The version gate, and the parse it guards. A capture that is refused shows the user a message
// instead of a scene, and one that is wrongly *accepted* shows a scene instead of a message -- which
// is worse, because it looks like an answer. Both directions are pinned here rather than by opening
// the panel, which needs a service, a campaign and a browser to say what these six cases say.
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CAPTURE_FORMAT, CAPTURE_VERSION, CaptureFormatError, openRunCapture } from './runCapture'

const URL = '/results/c/cfg/0/capture/capture.json'

/** A two-sample capture: one f8 time track, one joint track, one pose track. */
function capture(over: Record<string, unknown> = {}) {
  const times = new Float64Array([0.5, 1.5])
  const joint = new Float32Array([0.25, 0.75])
  // pos xyz then quat wxyz, sample-major.
  const pose = new Float32Array([1, 2, 3, 1, 0, 0, 0, 4, 5, 6, 0, 1, 0, 0])
  const bin = new Uint8Array(times.byteLength + joint.byteLength + pose.byteLength)
  bin.set(new Uint8Array(times.buffer), 0)
  bin.set(new Uint8Array(joint.buffer), times.byteLength)
  bin.set(new Uint8Array(pose.buffer), times.byteLength + joint.byteLength)

  const manifest: Record<string, unknown> = {
    format: CAPTURE_FORMAT,
    version: CAPTURE_VERSION,
    complete: true,
    frame: 'world',
    producer: 'roqsim',
    world: '/config/world/nav2.yaml',
    overrides: {},
    time: { base: 'sim', off: 0, dtype: 'f8', samples: 2, width: 1 },
    tracks: [
      { kind: 'joint', name: 'wheel_left_joint', unit: 'rad', width: 1, samples: 2, dtype: 'f4',
        off: times.byteLength },
      { kind: 'pose', name: 'base_footprint', width: 7, samples: 2, dtype: 'f4',
        off: times.byteLength + joint.byteLength },
    ],
    ...over,
  }
  // `version: undefined` in the overrides means "no version key at all", which is a case of its own.
  if ('version' in over && over.version === undefined) delete manifest.version
  return { manifest, bin }
}

/** Serve one capture over `fetch`, by the sibling convention the reader uses for capture.bin. */
function serve(over: Record<string, unknown> = {}) {
  const { manifest, bin } = capture(over)
  vi.stubGlobal('fetch', async (url: string) =>
    url.endsWith('capture.bin')
      ? { ok: true, arrayBuffer: async () => bin.buffer }
      : { ok: true, json: async () => manifest },
  )
}

afterEach(() => vi.unstubAllGlobals())

describe('the format version gate', () => {
  it('opens a capture at the version this reader implements', async () => {
    serve({ version: CAPTURE_VERSION })
    const source = await openRunCapture(URL)
    expect(source.range()).toMatchObject({ t0: 0.5, t1: 1.5, complete: true })
  })

  // Written as a LITERAL, unlike the cases around it. Every other test here is relative to
  // `CAPTURE_VERSION` and so passes at any value of it -- including 1, which is the bug this file
  // exists to catch: roqsim shipped v2 captures for months against a reader stuck at v1, and every
  // campaign's 3D panel showed a refusal. A literal is what fails when the two drift again. It has
  // to be edited when a producer moves, which is precisely the moment somebody must confirm this
  // reader was taught the new version rather than left behind by it.
  it('opens the version roqsim currently produces', async () => {
    serve({ version: 2 })
    await expect(openRunCapture(URL)).resolves.toBeDefined()
  })

  it('still opens v1, whose motion is identical -- v2 changed only what `overrides` means', async () => {
    serve({ version: 1, overrides: { components: { robot: { lidar: { rays: 4 } } } } })
    const source = await openRunCapture(URL)
    expect(source.tracks()).toHaveLength(2)
  })

  it('treats an absent version as the oldest format rather than failing', async () => {
    serve({ version: undefined })
    await expect(openRunCapture(URL)).resolves.toBeDefined()
  })

  it('refuses a newer version by name, and says what would fix it', async () => {
    serve({ version: CAPTURE_VERSION + 1 })
    // Both halves matter: the number, so the reader can be told which version to learn, and the
    // pointer to the spec, because "re-export with an older producer" was advice nobody could take.
    await expect(openRunCapture(URL)).rejects.toThrow(
      new RegExp(`version ${CAPTURE_VERSION + 1} is newer[\\s\\S]*run_capture\\.rst`),
    )
    await expect(openRunCapture(URL)).rejects.toBeInstanceOf(CaptureFormatError)
  })

  it('refuses a document that is not a run capture at all', async () => {
    serve({ format: 'something.else' })
    await expect(openRunCapture(URL)).rejects.toThrow(/not a run capture/)
  })
})

describe('what the gate is guarding', () => {
  // Without this the fixture above could be empty and every version case would pass vacuously.
  it('applies the joint and pose tracks of the sample nearest a time', async () => {
    serve()
    const source = await openRunCapture(URL)
    const joints: Record<string, number> = {}
    const poses: Record<string, number[]> = {}
    const sink = {
      joint: (name: string, value: number) => { joints[name] = value },
      pose: (name: string, pos: ArrayLike<number>, quat: readonly number[]) => {
        poses[name] = [...Array.from(pos), ...quat]
      },
    }
    source.apply(source.indexAt(1.4), sink)
    expect(joints.wheel_left_joint).toBeCloseTo(0.75)
    expect(poses.base_footprint).toEqual([4, 5, 6, 0, 1, 0, 0])
  })

  it('reports the world the motion was recorded from, which names a mismatch', async () => {
    serve()
    const source = await openRunCapture(URL)
    expect(source.meta()).toMatchObject({ producer: 'roqsim', world: '/config/world/nav2.yaml' })
  })
})
