// SVG requires its namespace constructor. `document.createElement('svg')` produces an
// HTML element without SVG geometry. The helper mirrors `h` arguments and selector
// grammar so chart and card construction stay consistent.

import { append } from './dom.js';

export const NS = 'http://www.w3.org/2000/svg';

/** `s('path.trace', {d, stroke}, ...children)` — `h()`, in the SVG namespace. */
export function s(spec, attrs, ...children) {
  const [tag, ...rest] = String(spec).split(/(?=[.#])/);
  const el = document.createElementNS(NS, tag || 'g');
  for (const token of rest) {
    // SVG classes use `classList`. Assigning to the read-only animated `className`
    // value can silently leave a chart unstyled.
    if (token[0] === '.') el.classList.add(token.slice(1));
    else el.setAttribute('id', token.slice(1));
  }
  if (attrs && attrs.constructor === Object) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
      else if (key === 'class') el.setAttribute('class', ((el.getAttribute('class') || '') + ' ' + value).trim());
      else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
      else if (key === 'dataset') Object.assign(el.dataset, value);
      else if (key === 'text') el.textContent = String(value);
      else el.setAttribute(key, value === true ? '' : String(value));
    }
  } else if (attrs !== null && attrs !== undefined) {
    children.unshift(attrs);
  }
  append(el, children);
  return el;
}

/**
 * The SVG root uses `viewBox` and `width: 100%` to scale with its card. Geometry and
 * type scale together, so models must choose dimensions and aspect deliberately to
 * keep labels legible.
 */
export function root(width, height, attrs, ...children) {
  return s('svg', {
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: 'xMidYMid meet',
    style: { display: 'block', width: '100%', overflow: 'visible' },
    ...(attrs || {}),
  }, ...children);
}
