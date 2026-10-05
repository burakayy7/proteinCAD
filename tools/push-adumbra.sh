#!/bin/sh
# Publish the viewer to the Adumbra website.
#
#     tools/push-adumbra.sh            check, then transfer
#     tools/push-adumbra.sh --check    check only, change nothing
#     ADUMBRA_DIR=~/src/adumbra tools/push-adumbra.sh
#
# There is no build step and no export folder: `web/` IS the deliverable. The
# app is plain ES modules with a checked-in three.js and an import map, so what
# runs from `python3 -m proteincad` is byte-for-byte what the site serves.
#
# The site wipes public/proteincad/ and copies web/ in verbatim, then re-applies
# two files of its own (adumbra.css, adumbra.js) for a mobile layout and a link
# home. It never edits ours. So the only way to break the site from this side is
# to move something it reaches for -- which is what the checks below are: a
# contract test for the handful of class names, ids and custom properties the
# site's patches are coupled to. They are cheap, and finding out from a phone
# that the layout collapsed is not.

set -eu

HERE=$(cd -- "$(dirname -- "$0")/.." && pwd)
APP="$HERE/web"
SITE=${ADUMBRA_DIR:-$(cd -- "$HERE/.." && pwd)/adumbra}
check_only=0
[ "${1:-}" = "--check" ] && check_only=1

problems=0
fail() { printf '  ✗ %s\n' "$1"; problems=$((problems + 1)); }
pass() { printf '  ✓ %s\n' "$1"; }

echo "checking $APP"

# --- the site's script refuses anything without this, and is right to --------
if [ -f "$APP/index.html" ]; then
  pass "index.html at the root of the folder"
else
  fail "no index.html at the root of $APP"
fi

# --- two tags get injected into these ---------------------------------------
for tag in '</head>' '</body>'; do
  if grep -q -- "$tag" "$APP/index.html" 2>/dev/null; then
    pass "index.html has $tag to inject into"
  else
    fail "index.html has no $tag — the site cannot wire itself in"
  fi
done

# --- served from /proteincad/, so an absolute path 404s on every asset -------
absolute=$(grep -rnE '(src|href)="/|url\(/|from "/|from '"'"'/|fetch\('"'"'/' \
  "$APP" --exclude-dir=vendor 2>/dev/null || true)
if [ -z "$absolute" ]; then
  pass "every path is relative"
else
  fail "absolute paths would 404 under /proteincad/:"
  printf '%s\n' "$absolute" | sed 's|^|      |'
fi

if grep -q '"three": "\./vendor/three\.module\.js"' "$APP/index.html" 2>/dev/null; then
  pass "the import map is relative"
else
  fail "the import map is not ./vendor/three.module.js"
fi

# --- the docs page, which is a second entry point and easy to forget ---------
if [ -f "$APP/docs.html" ]; then
  if grep -q 'href="\./docs\.html"' "$APP/index.html"; then
    pass "docs.html ships, and the viewer links to it"
  else
    fail "docs.html is here but nothing in the viewer links to it"
  fi
  grep -q 'href="\./index\.html"' "$APP/docs.html" \
    && pass "and it links back to the viewer" \
    || fail "docs.html has no relative link back to ./index.html"
else
  fail "no docs.html — the site expects a docs page at /proteincad/docs.html"
fi

# --- the deployment's own address, if this is a hosted push ------------------
#
# config.json is what turns the viewer from "talk to my own Python server" into
# "talk to that API Gateway, with a Cognito token". It is gitignored here --
# it belongs to one deployment rather than to the code -- but it DOES belong in
# the site, so this checks it rather than excluding it.
if [ -f "$APP/config.json" ]; then
  if python3 -c "import json,sys; json.load(open('$APP/config.json'))" 2>/dev/null; then
    pass "config.json is valid JSON"
  else
    fail "config.json is not valid JSON — the viewer would fall back to relative URLs"
  fi
  # Nothing in web/ is private. Everything here is downloadable by anyone who
  # loads the page, so a key in this file is a published key.
  leaked=$(grep -oiE '(aws_secret|aws_access|secret[_a-z]*|password|private_key|AKIA[0-9A-Z]{16})' \
    "$APP/config.json" 2>/dev/null || true)
  if [ -z "$leaked" ]; then
    pass "config.json carries no secret (it could not keep one)"
  else
    fail "config.json looks like it contains a credential:$leaked"
    fail "  everything in web/ is served to anyone who loads the page"
  fi
else
  # Not a failure -- a folder with no config.json is exactly right for a local
  # or Colab copy. But it is the wrong thing to put on a website, and the way
  # it goes wrong is quiet: the viewer falls back to relative `api/` URLs,
  # a static host 404s them, and the Design tab reads as a server outage.
  printf '  ! no config.json\n'
  printf '      The viewer will call relative api/ URLs, which a static host\n'
  printf '      cannot answer. On the live site that means:\n'
  printf '        - no Account section, so no Create account or Sign in\n'
  printf '        - no GPU machine section\n'
  printf '        - "the proteinCAD API is not answering" under Design\n'
  printf '      Viewing structures, styles, selection and export all still work.\n'
  printf '      To fix: deploy the stack, then write web/config.json from its\n'
  printf '      outputs -- step 6 of deploy/aws/SERVERLESS-DEPLOY.md.\n'
fi

# --- names the site reserves for its own two files --------------------------
if [ -e "$APP/adumbra.css" ] || [ -e "$APP/adumbra.js" ]; then
  fail "web/ ships adumbra.css or adumbra.js — the site overwrites those"
else
  pass "no collision with the site's own files"
fi

# --- what the site's mobile layout and back-link reach for -------------------
missing=""
for hook in 'class="workspace"' 'class="topbar"' 'class="panel left"' \
            'class="panel right"' 'class="panel-head"' 'id="collapse-left"' \
            'id="collapse-right"' 'class="group right"'; do
  grep -q -- "$hook" "$APP/index.html" || missing="$missing $hook"
done
for hook in '\.no-left' '\.no-right' '\.btn' '\.dialog' '\.shortcuts'; do
  grep -qE -- "$hook" "$APP/styles.css" || missing="$missing $hook"
done
for var in --line --text --muted --radius --accent-dim; do
  grep -q -- "  $var:" "$APP/styles.css" || missing="$missing $var"
done
if [ -z "$missing" ]; then
  pass "the hooks the site's patches use are all still here"
else
  fail "the site's patches reach for these, and they are gone:$missing"
  fail "  tell whoever maintains adumbra/patches/proteincad/ before pushing"
fi

# --- the app's own tests, the fast half (the Python side is not shipped) -----
if command -v node >/dev/null 2>&1; then
  if node "$HERE/tools/check.mjs" >/dev/null 2>&1; then
    pass "node tools/check.mjs"
  else
    fail "node tools/check.mjs fails — run it to see why"
  fi
fi

size=$(du -sh "$APP" | cut -f1)
echo "  · payload $size, $(find "$APP" -type f | wc -l | tr -d ' ') files"

if [ "$problems" -gt 0 ]; then
  printf '\n%s problem(s); nothing was transferred.\n' "$problems"
  exit 1
fi
[ "$check_only" -eq 1 ] && { printf '\nchecks only — nothing transferred.\n'; exit 0; }

# --- hand it over -----------------------------------------------------------
if [ ! -f "$SITE/package.json" ]; then
  printf '\nno website checkout at %s\n' "$SITE"
  echo "Set ADUMBRA_DIR to where it is:  ADUMBRA_DIR=/path/to/adumbra $0"
  exit 1
fi

printf '\ntransferring to %s\n' "$SITE"
cd "$SITE"
npm run proteincad -- "$APP"

cat <<EOF

Done — public/proteincad/ now matches web/.

The site build is a separate step, in $SITE:

  npm run dev       look at it locally, at /proteincad/
  git add -A public/proteincad && git commit && git push    deploys it
EOF
