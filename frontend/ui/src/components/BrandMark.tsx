import SvgIcon, { type SvgIconProps } from '@mui/material/SvgIcon'
import { accent } from '@/colors'

// The RoboVAST mark: a 2x2 grid of variation cells, one of them a robot face, one of them
// carrying a check — the framework's shape, a matrix of configurations that either pass or don't.
// The same geometry as the marketing site's header mark and the favicon, so the three read as one
// product; kept as inline SVG rather than an <img> of public/favicon.svg because that file bakes in
// its own background and stroke colour, while here the mark must take the colour of whatever it
// sits next to. Hence `currentColor` throughout and no background: at 24px on our near-black paper
// the filled square is what makes the cells legible, and it is the one tint that must stay
// literal — 'currentColor' at 12% alpha is not expressible. It is the site's own
// `.brand-mark rect` fill, so the two marks match exactly.
export function BrandMark(props: SvgIconProps) {
  return (
    <SvgIcon viewBox="0 0 40 40" {...props}>
      <g fill={accent(0.12)} stroke="currentColor" strokeWidth={1.5}>
        <rect x="3" y="4" width="16" height="15" rx="5" />
        <rect x="23" y="4" width="14" height="15" rx="4" />
        <rect x="3" y="23" width="16" height="14" rx="4" />
        <rect x="23" y="23" width="14" height="14" rx="4" />
      </g>
      <g fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth={1.7}>
        {/* antennae */}
        <path d="M8 4V1.5M14 4V1.5" />
        {/* the check, in the last cell */}
        <path d="m26.5 30 3 3 5-6" strokeLinejoin="round" strokeWidth={2} />
      </g>
      <g fill="currentColor">
        <circle cx="8.5" cy="11.5" r="1.3" />
        <circle cx="13.5" cy="11.5" r="1.3" />
      </g>
    </SvgIcon>
  )
}
