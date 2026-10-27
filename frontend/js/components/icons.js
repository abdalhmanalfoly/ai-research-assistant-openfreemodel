/* Inline SVG icons (Feather-style strokes). Built with createElementNS, no innerHTML. */
const P = (d) => ['path', { d }];
const L = (x1, y1, x2, y2) => ['line', { x1, y1, x2, y2 }];
const PL = (points) => ['polyline', { points }];
const C = (cx, cy, r) => ['circle', { cx, cy, r }];
const R = (x, y, width, height, rx = 0) => ['rect', { x, y, width, height, rx }];

const ICONS = {
  chat: [P('M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z')],
  library: [P('M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z'), P('M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z')],
  upload: [PL('16 16 12 12 8 16'), L(12, 12, 12, 21), P('M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3')],
  jobs: [PL('22 12 18 12 15 21 9 3 6 12 2 12')],
  status: [R(2, 2, 20, 8, 2), R(2, 14, 20, 8, 2), L(6, 6, 6.01, 6), L(6, 18, 6.01, 18)],
  settings: [L(4, 21, 4, 14), L(4, 10, 4, 3), L(12, 21, 12, 12), L(12, 8, 12, 3), L(20, 21, 20, 16), L(20, 12, 20, 3), L(1, 14, 7, 14), L(9, 8, 15, 8), L(17, 16, 23, 16)],
  menu: [L(3, 12, 21, 12), L(3, 6, 21, 6), L(3, 18, 21, 18)],
  sun: [C(12, 12, 5), L(12, 1, 12, 3), L(12, 21, 12, 23), L(4.22, 4.22, 5.64, 5.64), L(18.36, 18.36, 19.78, 19.78), L(1, 12, 3, 12), L(21, 12, 23, 12), L(4.22, 19.78, 5.64, 18.36), L(18.36, 5.64, 19.78, 4.22)],
  moon: [P('M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z')],
  copy: [R(9, 9, 13, 13, 2), P('M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1')],
  refresh: [PL('23 4 23 10 17 10'), PL('1 20 1 14 7 14'), P('M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15')],
  send: [L(22, 2, 11, 13), ['polygon', { points: '22 2 15 22 11 13 2 9 22 2' }]],
  stop: [R(5, 5, 14, 14, 2)],
  close: [L(18, 6, 6, 18), L(6, 6, 18, 18)],
  check: [PL('20 6 9 17 4 12')],
  alert: [P('M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z'), L(12, 9, 12, 13), L(12, 17, 12.01, 17)],
  info: [C(12, 12, 10), L(12, 16, 12, 12), L(12, 8, 12.01, 8)],
  file: [P('M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'), PL('14 2 14 8 20 8'), L(16, 13, 8, 13), L(16, 17, 8, 17)],
  collapse: [PL('11 17 6 12 11 7'), PL('18 17 13 12 18 7')],
  left: [PL('15 18 9 12 15 6')], right: [PL('9 18 15 12 9 6')],
  external: [P('M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6'), PL('15 3 21 3 21 9'), L(10, 14, 21, 3)],
  search: [C(11, 11, 8), L(21, 21, 16.65, 16.65)],
  plus: [L(12, 5, 12, 19), L(5, 12, 19, 12)],
  trash: [PL('3 6 5 6 21 6'), P('M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6'), P('M10 11v6M14 11v6'), P('M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2')],
};

export function icon(name, cls = 'icon') {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  for (const [k, v] of Object.entries({ viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false', class: cls })) svg.setAttribute(k, v);
  for (const [tag, attrs] of ICONS[name] ?? []) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    svg.append(n);
  }
  return svg;
}
