// Serialized into the top-level document by both browser implementations.
// Never return values or interpolate page errors into the tool response.
globalThis.permissiveCredentialFill = function ({ origin, selector, field, value, expiresAt, formOrigins = [origin], actionDeadline }, dom = globalThis.permissiveDOM()) {
  if (location.protocol !== 'https:' || location.origin !== origin ||
      (actionDeadline !== undefined && Date.now() >= actionDeadline) ||
      (expiresAt !== null && Date.now() >= expiresAt * 1000)) throw new Error('Credential is unavailable for this page');
  const element = dom.input(selector, field);
  if (dom.formOrigins(element).some(destination => !formOrigins.includes(destination))) throw new Error('Sign-in form destination changed; inspect and retry secure fill');
  const realm = element.ownerDocument.defaultView;
  const setter = Object.getOwnPropertyDescriptor(realm.HTMLInputElement.prototype, 'value').set;
  setter.call(element, value);
  element.dispatchEvent(new realm.Event('input', { bubbles: true, composed: true }));
  element.dispatchEvent(new realm.Event('change', { bubbles: true, composed: true }));
  return { ok: true };
};
