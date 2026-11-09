globalThis.permissiveInspect = function ({ selector = '', offset = 0, limit = 8000, elementOffset = 0, elementLimit = 100 } = {}, dom = globalThis.permissiveDOM()) {
  const root = selector ? dom.one(selector) : document.body;
  if (!root) throw new Error('Page is still loading; wait and inspect again');
  for (const [ref, node] of window.__permissiveDOMRefs.nodes) if (!node.isConnected) window.__permissiveDOMRefs.nodes.delete(ref);
  const parts = [], frames = [];
  for (const tree of dom.roots(root)) {
    const walker = document.createTreeWalker(tree, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const parent = node.parentElement;
      if (parent && !parent.closest('script,style,noscript') && dom.visible(parent)) {
        const text = node.textContent.replace(/\s+/g, ' ').trim();
        if (text) parts.push(text);
      }
      if (parts.length > 30000) break;
    }
    for (const frame of tree.querySelectorAll('iframe')) frames.push({ ...dom.reference(frame), title: frame.title, url: frame.src, accessible: !!frame.contentDocument });
  }
  const text = parts.join(' ').replace(/[ \t]+/g, ' ');
  limit = Math.max(1, Math.min(16000, limit));
  const elements = [], interactive = 'a[href],button,input,textarea,select,[contenteditable="true"],[role="listbox"],[role="button"],[role="tab"],[role="option"],[role="menuitem"],[role="checkbox"],[role="textbox"],[role="combobox"],[role="treeitem"]';
  const nodes = dom.queryAll(interactive, root);
  if (root.matches?.(interactive)) nodes.unshift(root);
  const priority = element => ({ listbox: 0, option: 1, tab: 2, textbox: 3, combobox: 3 }[dom.role(element)] ?? 4);
  nodes.sort((a, b) => priority(a) - priority(b));
  const visible = nodes.filter(dom.visible);
  elementLimit = Math.max(1, Math.min(200, elementLimit));
  for (const element of visible.slice(elementOffset, elementOffset + elementLimit)) {
    elements.push({ ...dom.reference(element), role: dom.role(element), name: dom.name(element),
      ...(element.tagName === 'INPUT' ? { type: element.type } : {}),
      ...(element.matches(':disabled') ? { disabled: true } : {}),
      ...(element.hasAttribute('aria-selected') ? { selected: element.getAttribute('aria-selected') === 'true' } : {}) });
  }
  return { url: location.href, title: document.title, offset, text: text.slice(offset, offset + limit),
    nextOffset: offset + limit < text.length ? offset + limit : null, totalCharacters: text.length, elements, frames,
    domTraversalTruncated: window.__permissiveDOMRefs.traversalTruncated,
    totalElements: visible.length, nextElementOffset: elementOffset + elementLimit < visible.length ? elementOffset + elementLimit : null };
};
