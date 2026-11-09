const { isIP } = require('node:net');

function validateTransport(target) {
  let url;
  try { url = new URL(target); }
  catch { throw new Error('Invalid API connection URL'); }
  const host = url.hostname.replace(/^\[|\]$/g, '');
  const loopback = host === 'localhost' || host === '::1' || (isIP(host) === 4 && host.startsWith('127.'));
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.hash || (url.protocol === 'http:' && !loopback)) {
    throw new Error('API connections require TLS or a loopback address, without URL credentials or fragments');
  }
  return url;
}

module.exports = { validateTransport };
