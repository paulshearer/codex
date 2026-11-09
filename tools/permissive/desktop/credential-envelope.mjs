const encode = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes)));
const decode = text => Uint8Array.from(atob(text), char => char.charCodeAt(0));

// Only this encrypted envelope crosses IPC, transport, or the controller.
export async function sealCredential(prepared, workspaceId, credential) {
  if (prepared.algorithm !== 'RSA-OAEP-256/A256GCM') throw new Error('Unsupported secure handoff');
  const publicKey = await crypto.subtle.importKey('spki', decode(prepared.publicKey),
    { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt']);
  const rawKey = crypto.getRandomValues(new Uint8Array(32));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const clear = new TextEncoder().encode(JSON.stringify(credential));
  try {
    const key = await crypto.subtle.importKey('raw', rawKey, 'AES-GCM', false, ['encrypt']);
    const wrappedKey = await crypto.subtle.encrypt('RSA-OAEP', publicKey, rawKey);
    const ciphertext = await crypto.subtle.encrypt({ name: 'AES-GCM', iv,
      additionalData: new TextEncoder().encode(prepared.uploadId + '\n' + workspaceId) }, key, clear);
    return { workspaceId, uploadId: prepared.uploadId, wrappedKey: encode(wrappedKey), iv: encode(iv), ciphertext: encode(ciphertext) };
  } finally { rawKey.fill(0); clear.fill(0); }
}
