// One rule: a name that is read has to be declared somewhere.
//
// `views/bench.js: dispose` once cleared `saveArmedTimer` — the name
// `views/pipeline.js` gives its own timer, never declared on the bench, whose
// timer had been renamed `armedTimer` in the same change. A module is strict,
// so the read threw a `ReferenceError` out of the router after the hash had
// moved: every click from the bench to another tab left the bench on screen,
// unsubscribed, until the page was reloaded (#82). Nothing under `tests/`
// mounts a view, so no test saw it; this does, without a browser:
//
//     npx eslint -c ui/eslint.config.mjs ui/lib ui/views ui/app.js ui/replay-page.js ui/tests
//
// `tests/test_ui.py` runs the same command from the Python suite and skips
// where there is no eslint. No style rules — `no-undef` is the whole check —
// and the globals are the ones the console actually reads, so a new one is
// added here on purpose rather than by a preset that names everything.
export default [{
  files: ['**/*.js', '**/*.mjs'],
  languageOptions: {
    ecmaVersion: 2024,
    sourceType: 'module',
    globals: Object.fromEntries([
      // the window
      'window', 'document', 'location', 'localStorage', 'console',
      'requestAnimationFrame', 'queueMicrotask', 'setTimeout', 'clearTimeout',
      'fetch', 'WebSocket', 'URL', 'Blob', 'getComputedStyle', 'ResizeObserver',
      'XMLSerializer', 'Node', 'SVGElement', 'globalThis',
      // node, in `tests/` and `lib/replay.js`
      'process',
    ].map((name) => [name, 'readonly'])),
  },
  rules: { 'no-undef': 'error' },
}];
