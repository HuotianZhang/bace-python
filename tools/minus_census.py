#!/usr/bin/env python3
"""Every minus sign on the console's screen, counted and measured (#68).

A grep over `ui/` cannot answer this. What a reader sees is a text node, in the
face the cascade actually resolved, at the size it actually resolved to — and
the two glyphs that matter here are laid out differently in a proportional face
and identically in a monospaced one. So this drives the real console in a real
browser, walks every text node, and measures each occurrence with a
one-character `Range`, which is the same thing the browser does when it lays a
column out.

It sorts every occurrence three ways:

  * **a sign** — one that opens a number (`-109 nA`) or an exponent's own
    (`3.65e-10`). This is the population `ui-rules` §2 is about;
  * **a hyphen** — inside a word, a path, a run label or a date. Not a sign
    and not this file's business (`s4_PTQ10IT4F`, `2026-08-07`, `jv-dark`);
  * **editable** — the value inside an `<input>`, which the operator types back
    and the console commits through `Number(raw)`. A real minus there is a
    defect, not a fix, so these are counted *as they should be*: ASCII.

The number to watch is the last line: **signs rendered with an ASCII hyphen
outside an input**. It was 81 across six screens before #68 and is 0 after.

    python -m bace.service --sim --fast --port 8900 --ui ui    # in one shell
    python3 tools/minus_census.py                              # in another

Needs `playwright` and a Chromium; neither is a dependency of anything else
here, which is why this is a tool and not a test. `--json out.json` keeps every
occurrence with its font, size, measured width and the DOM path it sat at, for
diffing one variant against another.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

# The one definition of "this hyphen is a sign", shared with the console's own
# `format.minusIn` — the tool that finds the defect and the function that fixes
# it must agree on what they are looking at.
WALK = r"""
() => {
  const HY = '-', MI = '−';
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    const s = n.nodeValue;
    if (!s || (!s.includes(HY) && !s.includes(MI))) continue;
    const el = n.parentElement;
    if (!el) continue;
    const cs = getComputedStyle(el);
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (ch !== HY && ch !== MI) continue;
      const before = s.slice(0, i), after = s.slice(i + 1);
      const sign = /^[0-9.]/.test(after)
        && (!/[0-9A-Za-z_]$/.test(before) || /[0-9][eE]$/.test(before));
      let w = null;
      try {
        const r = document.createRange();
        r.setStart(n, i); r.setEnd(n, i + 1);
        w = Math.round(r.getBoundingClientRect().width * 1000) / 1000;
      } catch (e) { /* detached between the walk and the measure */ }
      const path = [];
      for (let p = el; p && p !== document.body && path.length < 5; p = p.parentElement) {
        const c = typeof p.className === 'string' ? p.className
                : (p.className && p.className.baseVal) || '';
        path.push(p.tagName.toLowerCase()
          + (c ? '.' + c.trim().split(/\s+/).slice(0, 2).join('.') : ''));
      }
      out.push({
        glyph: ch === MI ? 'U+2212' : 'U+002D', sign, editable: false,
        text: s.length > 48 ? s.slice(0, 48) + '…' : s,
        font: cs.fontFamily.split(',')[0].replace(/['"]/g, ''),
        size: parseFloat(cs.fontSize), width: w, path: path.join(' < '),
      });
    }
  }
  // An input holds its value off the text tree entirely, and it is the one
  // place a real minus would be the defect.
  for (const inp of document.querySelectorAll('input')) {
    const v = inp.value || '';
    for (let i = 0; i < v.length; i++) {
      const ch = v[i];
      if (ch !== HY && ch !== MI) continue;
      const cs = getComputedStyle(inp);
      out.push({ glyph: ch === MI ? 'U+2212' : 'U+002D',
        sign: /^[0-9.]/.test(v.slice(i + 1)), editable: true, text: v,
        font: cs.fontFamily.split(',')[0].replace(/['"]/g, ''),
        size: parseFloat(cs.fontSize), width: null,
        path: 'input[' + (inp.getAttribute('aria-label') || inp.type) + ']' });
    }
  }
  return out;
}
"""


def settle(page, quiet=1200, cap=45000):
    """Until the DOM stops changing. A run in flight tears its card down and
    builds it again, and a census taken in the middle of that is a census of a
    loading state."""
    page.evaluate("""() => {
      window.__last = Date.now();
      if (window.__obs) window.__obs.disconnect();
      window.__obs = new MutationObserver(() => { window.__last = Date.now(); });
      window.__obs.observe(document.body, { subtree: true, childList: true, characterData: true });
    }""")
    waited = 0
    while waited < cap:
        page.wait_for_timeout(300)
        waited += 300
        if page.evaluate("() => Date.now() - window.__last") >= quiet:
            return True
    return False


def click(page, text, nth=0, label='', arm=40000):
    """Waits for the button to arm rather than asking once — a census that
    silently skipped a verb measured a different console each time it ran."""
    loc = page.locator('button', has_text=text)
    waited = 0
    while waited <= arm:
        try:
            if loc.count() > nth and loc.nth(nth).is_enabled():
                loc.nth(nth).click(timeout=8000)
                settle(page)
                return True
        except Exception as exc:                       # a re-render under the click
            print(f'# {label or text}: {type(exc).__name__}', file=sys.stderr)
        page.wait_for_timeout(500)
        waited += 500
    print(f'# {label or text}: never armed in {arm} ms', file=sys.stderr)
    return False


def drive(page, base, rows):
    """The six screens, in the order that reaches all of them.

    `bace` refuses to start unless the last sweep in scope measured a V_oc, so
    the light sweep runs first and the dark one last; put the dark `jv` in
    between and the two verbs below it never arm.
    """
    def snap(label):
        found = page.evaluate(WALK)
        for r in found:
            r['screen'] = label
        rows.extend(found)
        print(f'  {label}: {len(found)}', file=sys.stderr)

    page.goto(base + '#/bench', wait_until='networkidle')
    settle(page)
    snap('bench')
    if click(page, 'Run', 1, 'jv_bace'):
        snap('bench/jv_bace')
    if click(page, 'Shot', 0, 'bace shot'):
        snap('bench/shot')
    if click(page, 'Scan', 0, 'bace scan'):
        snap('bench/scan')
    if click(page, 'Run', 0, 'jv dark'):
        snap('bench/jv_dark')
    # A hash change, not a navigation: `goto` to the same document with a
    # different fragment does not re-run the router.
    for tab in ('#/pipeline', '#/results', '#/rig'):
        page.evaluate("(t) => { location.hash = t; }", tab)
        settle(page)
        snap(tab[2:])


def report(rows):
    signs = [r for r in rows if r['sign']]
    bad = [r for r in signs if r['glyph'] == 'U+002D' and not r['editable']]
    inputs = [r for r in rows if r['editable']]

    print(f'\n{len(rows)} minus glyphs on screen · {len(signs)} of them signs · '
          f'{len(inputs)} inside an input')
    if bad:
        print(f'\n{len(bad)} signs rendered with an ASCII hyphen, by site:')
        by = collections.defaultdict(list)
        for r in bad:
            by[r['path']].append(r)
        for path, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
            r = rs[0]
            print(f'  ×{len(rs):<4} {r["font"][:14]:14s} {r["size"]:>5}px  {r["text"][:48]!r}')
            print(f'         {path}')
    else:
        print('\nno sign is rendered with an ASCII hyphen — `ui-rules` §2 holds')

    wrong = [r for r in inputs if r['glyph'] == 'U+2212']
    if wrong:
        print(f'\n{len(wrong)} inputs carry a U+2212, which `Number()` will not parse:')
        for r in wrong:
            print(f'  {r["path"]}  {r["text"]!r}')
    else:
        print(f'every one of the {len(inputs)} signs inside an input is ASCII, as it must be')
    return 1 if (bad or wrong) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--base', default='http://127.0.0.1:8900/ui/',
                    help='the console, served by `python -m bace.service --sim --fast --ui ui`')
    ap.add_argument('--json', help='write every occurrence here, for diffing two variants')
    ap.add_argument('--chromium', default=os.environ.get('CHROMIUM_PATH'),
                    help='an explicit browser path, where playwright cannot find its own')
    args = ap.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('needs playwright: pip install playwright && playwright install chromium',
              file=sys.stderr)
        return 2

    rows = []
    with sync_playwright() as p:
        launch = {'args': ['--no-sandbox']}
        if args.chromium:
            launch['executable_path'] = args.chromium
        browser = p.chromium.launch(**launch)
        # The console is desktop-only and 1920 wide is what the lab PC runs
        # (`ui-rules` §1), so nothing here is measured at a width nobody uses.
        page = browser.new_page(viewport={'width': 1920, 'height': 1080})
        try:
            drive(page, args.base, rows)
        finally:
            browser.close()

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(rows, fh, indent=1, ensure_ascii=False)
        print(f'\n{args.json}: {len(rows)} occurrences', file=sys.stderr)
    return report(rows)


if __name__ == '__main__':
    raise SystemExit(main())
