// The DOM helpers support a console with no framework or build step, allowing direct
// use on the lab PC. `h` constructs elements and callers own subscriptions.

/**
 * The element constructor accepts selector, attributes and children through
 * `h('div.card', {onclick}, ...children)`.
 */
export function h(spec, attrs, ...children) {
  const [tag, ...rest] = String(spec).split(/(?=[.#])/);
  const el = document.createElement(tag || 'div');
  for (const token of rest) {
    if (token[0] === '.') el.classList.add(token.slice(1));
    else el.id = token.slice(1);
  }
  if (attrs && (attrs.constructor === Object)) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
      else if (key === 'class') el.className += (el.className ? ' ' : '') + value;
      else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
      else if (key === 'dataset') Object.assign(el.dataset, value);
      else if (key === 'text') el.textContent = String(value);
      else if (key === 'html') el.innerHTML = value;
      else el.setAttribute(key, value === true ? '' : String(value));
    }
  } else if (attrs !== null && attrs !== undefined) {
    children.unshift(attrs);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

/** The helper replaces an element's children together. */
export function fill(el, ...children) {
  el.textContent = '';
  return append(el, children);
}

/**
 * `keyed` rebuilds an element only when its model changes. Stream batches can arrive
 * every animation frame; rebuilding unchanged content wastes work and destroys text
 * selection, focus and the caret. A 21 × 60 scan measured on this bench creates 99
 * 421 rail elements without this guard even though the rail changes twice. Pure
 * models serve as render keys without manual invalidation. `keyed` exclusively owns
 * its element's children: external mutations leave the cached key describing absent
 * content, so a matching call cannot restore it. Independently managed card
 * containers must never also use `keyed`.
 */
export function keyed(el, key, build) {
  if (el.__key === key) return false;
  el.__key = key;
  fill(el, build());
  return true;
}

/** A number, in the mono face, with tabular figures — see `docs/ui-rules.md` §2. */
export function num(text, extra) {
  return h('span.num' + (extra ? '.' + extra : ''), { text: text === null || text === undefined ? '—' : text });
}
