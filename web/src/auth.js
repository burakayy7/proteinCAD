// Signing in, when the deployment asks for it.
//
// Cognito's hosted login, using the authorization-code flow with PKCE. The
// whole module is inert unless config.json carries an `auth` block, which is
// what keeps `python3 -m proteincad` on a laptop exactly as it was: no login,
// no redirect, nothing to configure.
//
// WHY PKCE
// --------
// The classic authorization-code flow proves the app is who it says it is with
// a client secret. A static page cannot hold a secret -- anyone can read it --
// so the Cognito client here is created without one. PKCE replaces the secret
// with a value invented per sign-in: a random `verifier` that never leaves this
// page, and its SHA-256 hash, which does. The code that comes back in the URL
// is worthless without the verifier, so intercepting the redirect is not enough
// to get a token.
//
// WHERE THE TOKENS GO
// -------------------
// sessionStorage, not localStorage: it is cleared when the tab closes, which is
// the closest thing to "signed out when you are done" that a browser offers
// without asking. It is still readable by script running on this origin, so it
// is worth knowing that this page loads nothing from anywhere else -- three.js
// is vendored, and there is no analytics, no tag manager and no CDN font.

let config = null;
let session = null;

const STORE = 'proteincad.session';
const PENDING = 'proteincad.signin';

/** Called once by api.js when config.json turned out to have an auth block. */
export async function configure(settings) {
  config = {
    domain: String(settings.domain || '').replace(/\/+$/, ''),
    clientId: String(settings.clientId || ''),
    // Must be one of the callback URLs on the Cognito client, exactly.
    redirect: String(settings.redirect || location.href.split('?')[0]),
    scope: settings.scope || 'openid email profile',
  };
  restore();
  await complete();
}

export function configured() {
  return Boolean(config && config.clientId);
}

/* ------------------------------------------------------------- the session */

function restore() {
  try {
    const saved = sessionStorage.getItem(STORE);
    if (saved) session = JSON.parse(saved);
  } catch {
    session = null;
  }
}

function keep(tokens) {
  session = {
    id: tokens.id_token,
    refresh: tokens.refresh_token || (session && session.refresh) || '',
    // A minute of slack, so a token that is about to expire is refreshed
    // rather than sent and rejected.
    expires: Date.now() + (Number(tokens.expires_in || 3600) - 60) * 1000,
    who: claims(tokens.id_token),
  };
  try { sessionStorage.setItem(STORE, JSON.stringify(session)); } catch { /* private mode */ }
  announce();
}

export function forget() {
  session = null;
  try { sessionStorage.removeItem(STORE); } catch { /* nothing to do */ }
  announce();
}

/** Who is signed in, or null. */
export function who() {
  return session ? session.who : null;
}

export function signedIn() {
  return Boolean(session && session.id);
}

/**
 * A token to send, refreshing it first if it is about to expire.
 *
 * Returns '' rather than throwing when nobody is signed in: an unauthenticated
 * request gets a 401 with a message the panel can show, which is a better
 * failure than an exception from inside a fetch helper.
 */
export async function token() {
  if (!configured() || !session) return '';
  if (Date.now() < session.expires) return session.id;
  if (!session.refresh) { forget(); return ''; }
  try {
    await exchange({ grant_type: 'refresh_token', refresh_token: session.refresh });
    return session ? session.id : '';
  } catch {
    forget();
    return '';
  }
}

/* --------------------------------------------------------------- the dance */

/**
 * Hand the browser to Cognito, at whichever of its pages we mean.
 *
 * Both pages take the same parameters and finish the same way -- a code on the
 * redirect back, exchanged in `complete()` -- so the only difference is which
 * form the person lands on.
 */
async function handOver(page) {
  if (!configured()) return;
  const verifier = random(64);
  const state = random(16);
  try {
    sessionStorage.setItem(PENDING, JSON.stringify({ verifier, state, from: location.href }));
  } catch { /* without this the callback cannot be completed; it will say so */ }

  const query = new URLSearchParams({
    response_type: 'code',
    client_id: config.clientId,
    redirect_uri: config.redirect,
    scope: config.scope,
    state,
    code_challenge: await challenge(verifier),
    code_challenge_method: 'S256',
  });
  location.assign(`${config.domain}${page}?${query}`);
}

export function signIn() {
  return handOver('/oauth2/authorize');
}

/**
 * Straight to the registration form.
 *
 * Cognito's sign-in page does carry a link to this, but only somebody who
 * already knows they need an account would think to press "Sign in" to find
 * it. `/signup` is a hosted-UI page in its own right and takes the same
 * parameters, so a button that says what it does costs nothing and removes the
 * one step where a first-time visitor has nothing to press.
 */
export function signUp() {
  return handOver('/signup');
}

export function signOut() {
  if (!configured()) { forget(); return; }
  forget();
  const query = new URLSearchParams({
    client_id: config.clientId,
    logout_uri: config.redirect,
  });
  location.assign(`${config.domain}/logout?${query}`);
}

/**
 * Finish a sign-in, if this page load is the one coming back from Cognito.
 *
 * Runs before anything asks for a token, and takes `code` and `state` back out
 * of the address bar afterwards -- partly so a reload does not try to spend a
 * code that has already been used, and partly because a one-time credential in
 * the URL is a thing that ends up in somebody's browser history.
 */
export async function complete() {
  if (!configured()) return;
  const query = new URLSearchParams(location.search);
  const code = query.get('code');
  if (!code) return;

  // Read both before anything touches the URL. `tidyUrl` empties this same
  // object -- it is the thing being rewritten -- so a comparison made after it
  // is a comparison against null, and every sign-in is refused for looking
  // like a forgery. That was the bug; hence the two consts.
  const state = query.get('state');

  let pending = null;
  try { pending = JSON.parse(sessionStorage.getItem(PENDING) || 'null'); } catch { /* none */ }
  sessionStorage.removeItem(PENDING);
  tidyUrl(query);

  if (!pending) {
    // No record of starting one. Usually this tab is not the tab that began
    // it -- sessionStorage is per-tab -- which happens if the login was
    // finished from a link in another window.
    problem = 'this tab did not start that sign-in. Press Sign in here and it will work.';
    announce();
    return;
  }
  if (pending.state !== state) {
    // A code arriving with a state we did not issue. Not ours to spend.
    problem = 'that sign-in did not start here, so it was not completed';
    announce();
    return;
  }

  try {
    await exchange({
      grant_type: 'authorization_code',
      code,
      redirect_uri: config.redirect,
      code_verifier: pending.verifier,
    });
  } catch (error) {
    problem = `could not finish signing in: ${error.message}`;
    announce();
  }
}

async function exchange(fields) {
  const body = new URLSearchParams({ client_id: config.clientId, ...fields });
  const response = await fetch(`${config.domain}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error_description || payload.error || `token endpoint said ${response.status}`);
  }
  keep(payload);
}

function tidyUrl(query) {
  query.delete('code');
  query.delete('state');
  // Whatever else was on the URL stays -- ?load=1CRN is a documented way in,
  // and losing it on the way back from a login would be a puzzle.
  const rest = query.toString();
  history.replaceState({}, '', location.pathname + (rest ? `?${rest}` : '') + location.hash);
}

/* --------------------------------------------------------------- the maths */

function random(length) {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return base64url(bytes);
}

async function challenge(verifier) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

function base64url(bytes) {
  let text = '';
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/**
 * The readable part of a JWT.
 *
 * Read for a name to show, never to decide anything. Whether the token is
 * valid is Cognito's answer to give and API Gateway's to check; a browser
 * reading its own token is reading a claim it was handed.
 */
function claims(jwt) {
  try {
    const body = String(jwt).split('.')[1];
    const padded = body.replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(decodeURIComponent(escape(atob(padded))));
  } catch {
    return {};
  }
}

/* ------------------------------------------------------------- listeners */

export let problem = '';

const listeners = new Set();

export function onChange(handler) {
  listeners.add(handler);
  return () => listeners.delete(handler);
}

function announce() {
  for (const handler of listeners) {
    try { handler(); } catch { /* a panel that threw is not our problem */ }
  }
}
