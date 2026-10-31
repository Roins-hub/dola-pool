/* Read only current composer controls, never history or dropdown alternatives. */
() => {
  const menu = '[role="menu"],[role="listbox"],[role="dialog"],'
    + '[data-radix-popper-content-wrapper],[data-message-id]';
  function visible(el) {
    if (!el) return false;
    const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
    return rect.width > 1 && rect.height > 1 && style.display !== 'none'
      && style.visibility !== 'hidden' && Number(style.opacity) !== 0;
  }
  function text(el) {
    const root = el.matches('button,[role="button"]') ? el
      : el.querySelector('button,[role="button"]') || el;
    const parts = [];
    function walk(node) {
      if (node.nodeType === 3) { parts.push(node.textContent); return; }
      if (node.nodeType !== 1 || (node !== root && (node.matches(menu) || !visible(node)))) return;
      for (const child of node.childNodes) walk(child);
    }
    walk(root);
    return (parts.join(' ').trim() || root.getAttribute('aria-label') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 120);
  }
  const models = /^(?:(?:Model|Modell|モデル|模型)\s*|(?:Dreamina\s+)?Seedance\s*)/i;
  const durations = /^(?:(?:Auto(?:matisch)?|自动|自動|\d+\s*:\s*\d+)\s*[·•・]?\s*)?\d+\s*(?:s\b|Sek|秒|seconds?\b)/i;
  const ratios = /^(?:(?:Ratio|Verhältnis|比率|比例|アスペクト比)\s*[:：]?\s*)?\d+\s*:\s*\d+(?:\s*[·•・]|$)/i;
  const editors = [...document.querySelectorAll('textarea,[contenteditable="true"],[role="textbox"]')]
    .filter(el => visible(el) && !el.closest(menu) && !el.disabled && !el.readOnly);
  const editor = editors.sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom)[0];
  if (!editor) return {modelText: '', durationText: '', ratioText: ''};
  const editorRect = editor.getBoundingClientRect();
  let composer = null;
  function controls(node) {
    return [...new Set([...node.querySelectorAll('button,[role="button"],[tabindex],span')]
      .filter(el => visible(el) && !el.closest(menu) && !editor.contains(el))
      // A combined params button can contain separate ratio/duration spans.
      // Read the button once, rather than treating its children as alternatives.
      .map(el => el.closest('button,[role="button"]') || el))];
  }
  // Legacy controls may lack control keys. Restrict fallback reads to a small
  // ancestor of the actual editor, rather than matching text across the page.
  // The 2026-10-22 ProseMirror viewport adds more than seven wrappers; keep a
  // finite depth plus the existing geometry fence and stop at another editor.
  for (let node = editor.parentElement, depth = 0; node && depth < 20; node = node.parentElement, depth++) {
    if (node === document.body || node === document.documentElement || node.closest(menu)) break;
    const rect = node.getBoundingClientRect();
    if (rect.height > editorRect.height + 350 || rect.width > editorRect.width + 300) break;
    if (editors.some(other => other !== editor && node.contains(other))) break;
    const candidates = controls(node);
    if (candidates.some(el => models.test(text(el))) && candidates.some(el => durations.test(text(el)))) {
      composer = node; break;
    }
  }
  function read(keys, pattern) {
    if (!composer) return '';
    const keyed = keys.flatMap(key => [...composer.querySelectorAll(
      `[data-input-engine-actionbar-control-key="${key}"]`)])
      .filter(el => visible(el) && !el.closest(menu));
    if (keyed.length) return keyed.length === 1 ? text(keyed[0]) : '';
    const labels = controls(composer).map(text).filter(value => pattern.test(value));
    const unique = [...new Set(labels)];
    return unique.length === 1 ? unique[0] : '';
  }
  const durationText = read(['video-duration'], durations);
  return {
    modelText: read(['video-model'], models),
    durationText,
    ratioText: read(['video-ratio', 'video-aspect', 'video-aspect-ratio'], ratios)
      || durationText.match(/\b\d+\s*:\s*\d+\b/)?.[0] || '',
  };
}
