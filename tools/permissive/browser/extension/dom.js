// Serialized into the page. One resolver for inspection, actions and secure fills.
globalThis.permissiveDOM = function () {
  const state = window.__permissiveDOMRefs ||= { prefix: crypto.randomUUID ? crypto.randomUUID().slice(0, 8) : Math.random().toString(16).slice(2, 10), counter: 0, nodes: new Map(), refs: new WeakMap() };
  state.traversalTruncated = false;
  const roots = (root = document) => {
    const found = [], pending = [root];
    while (pending.length && found.length < 2000) {
      const current = pending.shift(); found.push(current);
      const nodes = [...(current.querySelectorAll?.('*') || [])];
      if (current.nodeType === 1) nodes.unshift(current);
      for (const node of nodes) {
        if (node.shadowRoot) pending.push(node.shadowRoot);
        if (node.tagName === 'IFRAME') { try { if (node.contentDocument) pending.push(node.contentDocument); } catch {} }
      }
    }
    if (pending.length) state.traversalTruncated = true;
    return found;
  };
  const queryAll = (selector, root = document) => {
    if (typeof selector !== 'string' || !selector.trim()) throw new Error('Use an element ref or selector from inspect');
    const ref = selector.match(/^(cp-[a-f0-9]+-\d+)$|^\[data-permissive-ref=["'](cp-[a-f0-9]+-\d+)["']\]$/);
    if (ref) {
      const node = state.nodes.get(ref[1] || ref[2]);
      if (!node?.isConnected) throw new Error('Element reference is stale; inspect the current page again');
      return [node];
    }
    const parts = selector.split(/\s*>>>?\s*/);
    if (parts.length > 1) {
      let nodes = queryAll(parts.shift(), root);
      for (const part of parts) nodes = nodes.flatMap(node => queryAll(part, node.shadowRoot || node.contentDocument || node));
      return [...new Set(nodes)];
    }
    try { return [...new Set(roots(root).flatMap(current => [...current.querySelectorAll(selector)]))]; }
    catch { throw new Error('Invalid selector; use an element ref from inspect instead'); }
  };
  const one = selector => {
    const nodes = queryAll(selector);
    if (nodes.length !== 1) throw new Error(nodes.length ? 'Selector matches multiple elements; inspect and choose one ref' : 'Element was not found; inspect the current page again');
    return nodes[0];
  };
  const visible = element => {
    const bounds = element.getBoundingClientRect(), style = element.ownerDocument.defaultView.getComputedStyle(element);
    return !!(bounds.width && bounds.height && style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0');
  };
  const reference = element => {
    let ref = state.refs.get(element);
    if (!ref) { ref = `cp-${state.prefix}-${++state.counter}`; state.refs.set(element, ref); }
    state.nodes.set(ref, element);
    element.setAttribute('data-permissive-ref', ref);
    return { ref, selector: `[data-permissive-ref="${ref}"]` };
  };
  const composedText = element => {
    const parts = [], pending = [element];
    for (let count = 0; pending.length && count < 1000; count++) {
      const node = pending.shift();
      if (node.nodeType === 3) { parts.push(node.textContent); continue; }
      if (node.nodeType === 1 && (node.matches('script,style') || node.getAttribute('aria-hidden') === 'true')) continue;
      const assigned = node.tagName === 'SLOT' ? node.assignedNodes({ flatten: true }) : [];
      pending.unshift(...(assigned.length ? assigned : (node.shadowRoot || node).childNodes));
    }
    return parts.join(' ').replace(/\s+/g, ' ').trim();
  };
  const name = element => {
    const labelled = (element.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => element.getRootNode().getElementById?.(id) || element.ownerDocument.getElementById(id)).filter(Boolean).map(node => node.textContent).join(' ');
    return (element.getAttribute('aria-label') || labelled || [...(element.labels || [])].map(label => label.textContent).join(' ') ||
      element.getAttribute('placeholder') || composedText(element) || element.getAttribute('title') || element.getAttribute('alt') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 400);
  };
  const role = element => element.getAttribute('role') || ({ A: 'link', BUTTON: 'button', TEXTAREA: 'textbox', SELECT: 'combobox' }[element.tagName]) ||
    (element.tagName === 'INPUT' ? ({ checkbox: 'checkbox', radio: 'radio', submit: 'button', button: 'button' }[element.type] || 'textbox') : element.tagName.toLowerCase());
  const input = (selector, field) => {
    const element = one(selector), realm = element.ownerDocument.defaultView;
    if (!(element instanceof realm.HTMLInputElement) || element.disabled || element.readOnly || !visible(element) ||
        (field === 'password' ? element.type !== 'password' : !['text', 'email', 'tel'].includes(element.type))) throw new Error('Select one visible sign-in input of the correct type');
    if (realm.location.origin !== location.origin) throw new Error('Sign-in input must belong to the current HTTPS page');
    return element;
  };
  const formOrigins = element => {
    const form = element.form, destinations = [element.ownerDocument.location.origin];
    if (form) {
      destinations.push(new URL(form.action || element.ownerDocument.location.href, element.ownerDocument.location.href).origin);
      const submitters = new Set([...form.elements, ...form.querySelectorAll('[formaction]'), ...element.ownerDocument.querySelectorAll('[formaction]')]);
      for (const button of submitters) if (button.form === form && button.hasAttribute('formaction')) destinations.push(new URL(button.formAction, element.ownerDocument.location.href).origin);
    }
    const result = [...new Set(destinations)];
    if (result.length > 32) throw new Error('Too many sign-in form destinations');
    return result;
  };
  const contains = (parent, node) => {
    for (let current = node; current; current = current.assignedSlot || current.parentElement || current.getRootNode()?.host) if (current === parent) return true;
    return false;
  };
  const acceptsHit = (element, hit) => {
    if (contains(element, hit)) return true;
    // Pointer events can be retargeted to an ancestor shadow host (AA's header
    // link does this). An unrelated overlay is still a different hit target.
    for (let host = element.getRootNode().host; host; host = host.getRootNode().host) if (host === hit) return true;
    return false;
  };
  return { roots, queryAll, one, visible, reference, name, role, input, formOrigins, contains, acceptsHit };
};

globalThis.permissiveExpression = (action, args) =>
  '(() => { const dom = (' + permissiveDOM.toString() + ')(); return (' + action.toString() + ')(' + JSON.stringify(args) + ', dom); })()';

globalThis.permissiveScript = script => {
  if (typeof script !== 'string' || !script.trim()) throw new Error('evaluate requires a JavaScript expression or function');
  return '(() => { let result; try { result = (0, eval)(' + JSON.stringify(script) + '); } catch (error) { if (error instanceof SyntaxError && /Illegal return/.test(error.message)) return (new Function(' + JSON.stringify(script) + '))(); throw error; } return typeof result === "function" ? result() : result; })()';
};

globalThis.permissiveAction = async function (args, dom = globalThis.permissiveDOM()) {
  const { operation, selector, text, field, direction = 'down' } = args;
  if (operation === 'wait') {
    const start = Date.now();
    do {
      try { const node = dom.one(selector); if (dom.visible(node)) return { ready: true }; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    } while (Date.now() - start < 20000);
    throw new Error('Element did not become visible; inspect the page again');
  }
  if (operation === 'credentialContext') {
    const element = dom.input(selector, field);
    return { origin: location.origin, formOrigins: dom.formOrigins(element) };
  }
  if (operation === 'scroll') {
    let element = selector ? dom.one(selector) : document.scrollingElement;
    const target = element;
    const scrollable = node => node?.clientHeight > 0 && node.scrollHeight > node.clientHeight + 1 &&
      (node === node.ownerDocument.scrollingElement || /auto|scroll|overlay/.test(node.ownerDocument.defaultView.getComputedStyle(node).overflowY));
    if (!scrollable(element)) element = dom.queryAll('*', target).find(scrollable) || element;
    while (element && !scrollable(element)) element = element.parentElement || element.getRootNode()?.host;
    if (!element) return { scrollTop: target.scrollTop, scrollable: false };
    element.scrollTop = direction === 'top' ? 0 : direction === 'bottom' ? element.scrollHeight : element.scrollTop + (direction === 'up' ? -1 : 1) * element.clientHeight * .8;
    return { scrollTop: element.scrollTop };
  }
  const element = dom.one(selector);
  if (args.actionDeadline !== undefined && Date.now() >= args.actionDeadline) throw new Error('Page action expired; inspect again');
  if (!dom.visible(element) || element.matches(':disabled')) throw new Error('Element is not visible or enabled; inspect the page again');
  if (operation === 'type') {
    if (element.type === 'password') throw new Error('Use fillCredential to fill a password securely');
    if (element.readOnly) throw new Error('Input is read-only');
    element.focus();
    const realm = element.ownerDocument.defaultView;
    if (element instanceof realm.HTMLInputElement || element instanceof realm.HTMLTextAreaElement) {
      const type = element instanceof realm.HTMLTextAreaElement ? realm.HTMLTextAreaElement : realm.HTMLInputElement;
      Object.getOwnPropertyDescriptor(type.prototype, 'value').set.call(element, text);
    } else if (element.isContentEditable) element.textContent = text;
    else throw new Error('Select an editable input from inspect');
    element.dispatchEvent(new realm.Event('input', { bubbles: true, composed: true }));
    element.dispatchEvent(new realm.Event('change', { bubbles: true, composed: true }));
    return { ok: true };
  }
  if (operation === 'clickPoint' || operation === 'click') {
    element.scrollIntoView({ block: 'center', inline: 'center' });
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const bounds = element.getBoundingClientRect();
    let x = bounds.left + bounds.width / 2, y = bounds.top + bounds.height / 2;
    let doc = element.ownerDocument, hit = doc.elementFromPoint(x, y);
    while (hit?.shadowRoot) { const child = hit.shadowRoot.elementFromPoint(x, y); if (!child || child === hit) break; hit = child; }
    if (!dom.acceptsHit(element, hit)) throw new Error('Element is covered by another control; inspect and dismiss the overlay first');
    while (doc.defaultView.frameElement) {
      const frame = doc.defaultView.frameElement, rect = frame.getBoundingClientRect();
      x += rect.left + frame.clientLeft; y += rect.top + frame.clientTop; doc = frame.ownerDocument;
      const parentHit = doc.elementFromPoint(x, y);
      if (!dom.acceptsHit(frame, parentHit)) throw new Error('Frame is covered; inspect the page again');
    }
    if (operation === 'click') {
      // Used only when a web tab's extension-owned frame prevents debugger
      // attachment. The same visibility, disabled and overlay checks apply.
      if (args.actionDeadline !== undefined && Date.now() >= args.actionDeadline) throw new Error('Page action expired; inspect again');
      hit.focus?.();
      hit.click();
      return { ok: true, interaction: 'dom', hint: 'Inspect to confirm the click; this tab cannot send trusted pointer events' };
    }
    return { x, y };
  }
  if (operation === 'link') return element.href;
  throw new Error('Unsupported DOM action');
};
