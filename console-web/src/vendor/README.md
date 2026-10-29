# Public Visual Dependencies

- Three.js 0.160.1: `https://cdn.jsdelivr.net/npm/three@0.160.1/build/three.module.min.js`. MIT license, complete notice retained in `three.js`. The local distribution is unchanged apart from expanding the license notice.
- Lucide Static 0.468.0: selected icon nodes from `https://cdn.jsdelivr.net/npm/lucide-static@0.468.0/icon-nodes.json`. Full upstream license retained in `lucide.js`. Regenerate the selection with `node scripts/vendor-public-icons.mjs` from `console-web`.

These resources are served locally and included in the verified production import graph. Page loads do not contact a CDN. Version updates require asset digest regeneration and frontend regression tests.
