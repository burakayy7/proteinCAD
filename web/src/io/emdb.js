// EMDB: recognising an id, and reading what the EBI says about an entry.
//
// Pure, like everything else under io/ -- no three.js and no api.js. Where this
// deployment's server is, and whether to ask it before asking the EBI, is the
// App's business and not this file's; what is here is the part that would be
// the same on any of them.

/** The map itself, gzipped, served with Access-Control-Allow-Origin: *. */
export const EBI_MAP_URL = 'https://ftp.ebi.ac.uk/pub/databases/emdb/structures/EMD-{id}/map/emd_{id}.map.gz';
/** The entry document: title, recommended contour level, fitted models. */
export const EBI_ENTRY_URL = 'https://www.ebi.ac.uk/emdb/api/entry/EMD-{id}';

/**
 * Is this an EMDB id, and which one?
 *
 * Deliberately narrow. A bare four-character token is a PDB id -- `4HHB`, and
 * `9N49` too -- so an EMDB id has to either carry its prefix or be a number too
 * long to be one. Guessing wrong means typing a PDB id and getting a density
 * map back, which is worse than being made to say which you meant.
 */
export function emdbId(text) {
  const token = String(text || '').trim();
  const prefixed = /^emd[-_ ]?(\d{3,6})$/i.exec(token);
  if (prefixed) return `EMD-${prefixed[1]}`;
  if (/^\d{5,6}$/.test(token)) return `EMD-${token}`;
  return null;
}

/** The digits on their own, which is what the URLs are built from. */
export function emdbNumber(id) {
  return String(id).replace(/^emd[-_ ]?/i, '');
}

export function mapUrl(id) {
  return EBI_MAP_URL.replace(/\{id\}/g, emdbNumber(id));
}

export function entryUrl(id) {
  return EBI_ENTRY_URL.replace(/\{id\}/g, emdbNumber(id));
}

/**
 * The useful part of EBI's entry document.
 *
 * The recommended contour level is the one worth having: it is the level the
 * depositors looked at the map at, and a map shown at the wrong level is either
 * a solid block or nothing at all.
 */
export function readEbiEntry(payload) {
  const map = (payload && payload.map) || {};
  const contours = (map.contour_list && map.contour_list.contour) || [];
  const primary = contours.find((c) => c.primary) || contours[0] || null;
  const level = primary ? Number(primary.level) : NaN;
  const references = (((payload && payload.crossreferences) || {}).pdb_list || {})
    .pdb_reference || [];

  return {
    title: ((payload && payload.admin) || {}).title || '',
    contour: Number.isFinite(level) ? level : null,
    // Models deposited against this map. Loading one on top is usually the
    // next thing somebody wants, so the tree offers it as a button.
    fitted: references.map((r) => r && r.pdb_id).filter(Boolean),
    resolution: resolutionOf(payload),
  };
}

/**
 * The reported resolution, wherever in the document it is this time.
 *
 * It lives under structure_determination -> image_processing ->
 * final_reconstruction -> resolution -> valueOf_, which is four levels of
 * nesting through two lists, and entries do not all agree on the depth. So this
 * looks for the key rather than pathing to it, breadth first, so the shallowest
 * match wins and the answer does not depend on key ordering.
 */
function resolutionOf(payload) {
  const queue = [payload];
  let visited = 0;
  while (queue.length && visited < 10000) {
    const node = queue.shift();
    visited++;
    if (Array.isArray(node)) {
      queue.push(...node);
    } else if (node && typeof node === 'object') {
      const raw = node.resolution;
      const value = raw && typeof raw === 'object' ? raw.valueOf_ : raw;
      const number = Number(value);
      if (value !== undefined && value !== null && Number.isFinite(number)) return number;
      queue.push(...Object.values(node));
    }
  }
  return null;
}
