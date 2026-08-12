import type { ClockSource } from './clock';
export declare function useCanvasClock(clock: ClockSource, draw: (ctx: CanvasRenderingContext2D, w: number, h: number, t: number) => void): {
    containerRef: import("react").MutableRefObject<HTMLDivElement | null>;
    canvasRef: import("react").MutableRefObject<HTMLCanvasElement | null>;
    requestDraw: () => void;
};
