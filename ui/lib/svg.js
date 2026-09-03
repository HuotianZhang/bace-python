// The SVG half of `dom.js`, and the reason it has to exist: `h()` builds every
// element with `document.createElement`, and `createElement('svg')` is an
// `HTMLUnknownElement` — a tag with the right name, no geometry, and nothing
// drawn. SVG lives in its own namespace, so it needs its own constructor.
//
// Everything else is deliberately the same as `h()`: the same
// `tag[.class][#id]` grammar, the same attrs-then-children shape, the same
// `keyed()` beside it. A chart is built the way a card is built.

import { append } from './dom.js';

export const NS = 'http://www.w3.org/2000/svg';

/** `s('path.trace', {d, stroke}, ...children)` — `h()`, in the SVG namespace. */
export function s(spec, attrs, ...children) {
  const [tag, ...rest] = String(spec).split(/(?=[.#])/);
  const el = document.createElementNS(NS, tag || 'g');
  for (const token of rest) {
    // `el.classList` works on SVG elements, but `className` does not: on an
    // SVGElement it is a read-only `SVGAnimatedString`, and assigning to it
    // fails silently in exactly the way that leaves a chart unstyled.
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
 * The root. `viewBox` with no width/height and `width: 100%` in the style is
 * what makes a chart resize with its card without re-running the model: the
 * geometry is computed once in the model's own coordinates and the browser
 * scales it. The model still owns the aspect ratio, because type inside an
 * SVG scales with it and 7 px labels are not a design.
 */
export function root(width, height, attrs, ...children) {
  return s('svg', {
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: 'xMidYMid meet',
    style: { display: 'block', width: '100%', overflow: 'visible' },
    ...(attrs || {}),
  }, ...children);
}
