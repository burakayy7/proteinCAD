// Where the server is, and how to talk to it.
//
// The viewer has always called relative URLs -- `api/health`, `api/design` --
// which is what lets the same folder run from `python3 -m proteincad`, from a
// Colab session and from a web server without being told where it is. That is
// still the default and still what happens when there is no config.json.
//
// A hosted deployment is the one case where the API is somewhere else: the page
// is a static folder on a CDN and the API is behind Cognito on API Gateway. So
// the address is configuration, read once at startup from a file next to
// index.html, and never written into the code:
//
//     web/config.json
//     {
//       "api": "https://abc123.execute-api.us-east-1.amazonaws.com",
//       "auth": {
//         "domain":   "https://proteincad-login.auth.us-east-1.amazoncognito.com",
//         "clientId": "1a2b3c4d5e6f7g8h9i0j",
//         "redirect": "https://adumbra.burakayy.com/proteincad/"
//       }
//     }
//
// None of those are secrets. A Cognito user pool client id is a public
// identifier, which is why the client is created without a secret at all: a
// static page cannot keep one. Nothing in web/ ever holds a credential.
//
// Every call goes through `request`, which is the one place that knows about
// the bearer token, so signing in is invisible to the rest of the app.

import * as auth from './auth.js';

const settings = { api: '', auth: null };

/**
 * Load config.json, then finish a sign-in if we came back from one.
 *
 * Kicked off at import and awaited by every request, so the rest of the app
 * never has to think about ordering: the first call that needs a token already
 * has one, and a reload in the middle of a redirect resolves before anything
 * asks.
 *
 * A missing config.json is the normal case, not an error. It means "you are
 * running this yourself", which is how it runs on a laptop and in Colab.
 */
export const ready = (async () => {
  // `?local=1` ignores config.json for this tab.
  //
  // One checkout is often both the thing being developed and the thing that was
  // deployed, and config.json belongs to the deployment. Without this, running
  // `python3 -m proteincad` in a checkout that has one serves the viewer from
  // your machine and then points it at the API Gateway -- so every route the
  // server grew since the last deploy reads as missing, which is indisputably
  // the most confusing way for a new endpoint to fail.
  if (ignoreConfig()) return settings;
  try {
    const response = await fetch('./config.json', { cache: 'no-store' });
    if (response.ok) {
      const loaded = await response.json();
      if (loaded && typeof loaded.api === 'string') settings.api = loaded.api.replace(/\/+$/, '');
      if (loaded && loaded.auth && loaded.auth.clientId) settings.auth = loaded.auth;
    }
  } catch {
    // No file, or something that is not JSON. Either way: relative URLs.
  }
  if (settings.auth) await auth.configure(settings.auth);
  return settings;
})();

function ignoreConfig() {
  if (typeof location === 'undefined') return false;
  const asked = new URLSearchParams(location.search).get('local');
  return asked !== null && asked !== '0' && asked !== 'false';
}

/** True when this page is talking to a hosted API rather than its own server. */
export function hosted() {
  return Boolean(settings.api);
}

/**
 * True when the page itself was served from this machine.
 *
 * Together with `hosted()` this distinguishes the two ways a route can be
 * missing: a real static deployment, where the feature genuinely is not there,
 * and a local checkout holding a deployment's config.json, where it is there
 * and the browser is simply not talking to it. Those want different advice, and
 * giving the second one the first one's advice sends somebody to run a server
 * they are already running.
 */
export function servedLocally() {
  if (typeof location === 'undefined') return false;
  return ['localhost', '127.0.0.1', '[::1]', '::1', ''].includes(location.hostname)
    || location.protocol === 'file:';
}

/** True when there is a sign-in to do at all. */
export function guarded() {
  return Boolean(settings.auth);
}

export function url(path) {
  const clean = String(path).replace(/^\/+/, '');
  // Relative when there is no configured API, which keeps the viewer working
  // from a file server, a laptop and a Colab tunnel unchanged.
  return settings.api ? `${settings.api}/${clean}` : `api/${clean}`;
}

/**
 * One fetch, with the token attached if there is one.
 *
 * Returns the Response rather than the parsed body, because the callers in
 * app.js read the status and the error body to decide what to say. The only
 * thing this adds is the header, and a note when the answer is "who are you".
 */
export async function request(path, options = {}) {
  await ready;
  const headers = new Headers(options.headers || {});
  const token = settings.auth ? await auth.token() : '';
  if (token) headers.set('Authorization', `Bearer ${token}`);

  const response = await fetch(url(path), { ...options, headers });

  if (response.status === 401 && settings.auth) {
    // The token expired or was never there. Nothing else in the app can tell
    // that apart from the server being down, so say it here.
    auth.forget();
    announce('signin');
  }
  return response;
}

export function get(path) {
  return request(path);
}

export function post(path, body) {
  return request(path, {
    method: 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/* --------------------------------------------------------------- listeners */

const listeners = new Set();

/** Called when the app should show a sign-in prompt. */
export function onSignIn(handler) {
  listeners.add(handler);
  return () => listeners.delete(handler);
}

function announce(what) {
  for (const handler of listeners) {
    try { handler(what); } catch { /* a panel that threw is not our problem */ }
  }
}

export { auth };
