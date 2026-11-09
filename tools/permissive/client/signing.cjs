// Private keys stay in the caller's protected store, outside the service home.
const { execFileSync } = require('node:child_process');
function createSigner({ python, home, env = process.env, scope = 'api' }) {
  return (method, target, body = '', headers = {}) => JSON.parse(execFileSync(python,
    ['-B', '-X', 'utf8', '-m', 'permissive.auth', '--home', home, 'sign', '--scope', scope],
    { env, windowsHide: true, encoding: 'utf8', input: JSON.stringify({ method, target,
      body: Buffer.from(body).toString('base64'), headers }), maxBuffer: 65536 }));
}
module.exports = { createSigner };
