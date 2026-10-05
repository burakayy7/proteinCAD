// Minimal DOM helpers. No framework: the UI is small and mostly lists.

/**
 * el('div.row', { title: 'x' }, child, child)
 * The tag may carry classes: 'button.btn.small'.
 */
export function el(spec, attrs = null, ...children) {
  const [tag, ...classes] = spec.split('.');
  const node = document.createElement(tag || 'div');
  if (classes.length) node.className = classes.join(' ');
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className += (node.className ? ' ' : '') + value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else if (key === 'value') node.value = value;
      else if (key === 'checked' || key === 'disabled' || key === 'hidden' || key === 'selected') node[key] = !!value;
      else node.setAttribute(key, value);
    }
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(node, child);
    else node.appendChild(typeof child === 'object' ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function $(selector, root = document) {
  return root.querySelector(selector);
}

export function options(select, items, selected) {
  clear(select);
  for (const item of items) {
    select.appendChild(el('option', { value: item.id, selected: item.id === selected }, item.label));
  }
  return select;
}

/** Format a number with a fixed number of decimals, without trailing noise. */
export function fixed(value, digits = 2) {
  return Number.isFinite(value) ? value.toFixed(digits) : '-';
}

/** Trailing-edge throttle, used for hover picking. */
export function throttle(fn, ms) {
  let last = 0;
  let timer = null;
  let pending = null;
  const run = () => {
    last = performance.now();
    timer = null;
    const args = pending;
    pending = null;
    if (args) fn(...args);
  };
  return (...args) => {
    pending = args;
    const elapsed = performance.now() - last;
    if (elapsed >= ms) run();
    else if (!timer) timer = setTimeout(run, ms - elapsed);
  };
}
