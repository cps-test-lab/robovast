// useCanvasClock: the shared canvas scaffolding for imperative, clock-driven panels. It owns the
// HiDPI-sized <canvas>, coalesces redraws onto requestAnimationFrame, and redraws on every clock
// change and on resize -- so a panel only writes its draw(ctx, w, h, t) and calls requestDraw() when
// its data finishes loading. Sizes are in device pixels (crisp on HiDPI); draw in those units.

import { useCallback, useEffect, useRef } from 'react'
import type { ClockSource } from './clock'

export function useCanvasClock(
  clock: ClockSource,
  draw: (ctx: CanvasRenderingContext2D, w: number, h: number, t: number) => void,
) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const sizeRef = useRef({ w: 0, h: 0 })
  const rafRef = useRef<number | null>(null)
  // Keep the latest draw closure without re-subscribing; the panel's draw reads its own refs/state.
  const drawRef = useRef(draw)
  drawRef.current = draw

  const requestDraw = useCallback(() => {
    if (rafRef.current != null) return
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null
      const canvas = canvasRef.current
      const ctx = canvas?.getContext('2d')
      if (!canvas || !ctx) return
      const { w, h } = sizeRef.current
      drawRef.current(ctx, w, h, clock.t)
    })
  }, [clock])

  // HiDPI sizing: back the canvas with device pixels, keep its CSS box at the container size.
  //
  // Checked after every render rather than once at mount, because the container is not always there
  // at mount: a panel that renders a loading state first attaches its canvas a render or more
  // later, and an observer set up only at mount would never size it -- the canvas stays 0 x 0 and
  // nothing it draws is seen. Re-observing only when the element changes keeps this to a comparison.
  const observed = useRef<{ container: HTMLDivElement | null; ro: ResizeObserver | null }>({
    container: null,
    ro: null,
  })
  useEffect(() => {
    const container = containerRef.current
    if (container === observed.current.container) return
    observed.current.ro?.disconnect()
    observed.current = { container, ro: null }
    if (!container) return
    const ro = new ResizeObserver(() => {
      const canvas = canvasRef.current
      if (!canvas) return
      const dpr = window.devicePixelRatio || 1
      const cw = container.clientWidth
      const ch = container.clientHeight
      sizeRef.current = { w: Math.round(cw * dpr), h: Math.round(ch * dpr) }
      canvas.width = sizeRef.current.w
      canvas.height = sizeRef.current.h
      canvas.style.width = `${cw}px`
      canvas.style.height = `${ch}px`
      requestDraw()
    })
    ro.observe(container)
    observed.current.ro = ro
  })
  useEffect(() => () => observed.current.ro?.disconnect(), [])

  // Follow the clock: coalesced redraw on every change (and once on mount).
  useEffect(() => {
    requestDraw()
    return clock.subscribe(requestDraw)
  }, [clock, requestDraw])

  return { containerRef, canvasRef, requestDraw }
}
