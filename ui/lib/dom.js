// The whole DOM layer. There is no framework and no build step: the lab PC
// has neither, and `docs/ui-plan.md` says so. What a framework would give us
// here is `h()` and a place to put a subscription, so that is what this is.

/** `h('div.card', {onclick}, ...children)` — tag[.class][#id], attrs, children. */
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

/** Replace an element's children in one go. */
export function fill(el, ...children) {
  el.textContent = '';
  return append(el, children);
}

/**
 * Rebuild `el` only when `key` differs from the key it was last built from.
 *
 * The store notifies on every batch of frames, and a `--sim --fast` scan
 * batches one per animation frame for the length of the run. Every renderer in
 * the shell is a pure function of the state, so without this the rail, the
 * chips and the strip are torn down and rebuilt sixty times a second to draw
 * the characters they already had — measured over one 21 x 60 scan: 99 421
 * elements built for a rail that changed twice.
 *
 * That is not only waste. A rebuilt element is a *different* element: it drops
 * the operator's text selection mid-copy, and from M2 it would take the focus
 * and the caret out of a parameter field the moment a frame arrived. So the
 * rule is the one the models already make possible — **the model is the render
 * key**: build it (it is cheap and pure), and touch the DOM only where it
 * differs. Nothing has to remember to invalidate anything, which is the one
 * thing a hand-maintained dirty flag always gets wrong.
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
