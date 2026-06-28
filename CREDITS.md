# Credits

LeetCoach is original work by yib7 under the [MIT License](LICENSE). It bundles two
third-party front-end libraries, vendored under `static/vendor/` so the app needs no
runtime CDN.

## Vendored libraries

| Library | Version | License | Used for |
| --- | --- | --- | --- |
| [marked](https://github.com/markedjs/marked) | 12.0.2 | MIT | Rendering Claude's markdown answer in the browser |
| [highlight.js](https://github.com/highlightjs/highlight.js) | 11.9.0 | BSD-3-Clause | Syntax highlighting (core + the `python`, `cpp`, `java` grammars and the `github-dark` theme) |

Both licenses permit redistribution. Their full license texts live in each project's
upstream repository linked above; the vendored files are unmodified minified builds.

## Design lineage

Two patterns were adapted from the author's own sibling projects (not third-party code):

- The throwaway-directory, secret-free, resource-capped sandbox runner in `sandbox.py`
  follows the approach used in STATlee.
- The server-sent-events streaming endpoint in `app.py` mirrors the injectable
  `stream_fn` pattern from the Xeno RAG project, translated from FastAPI to Flask.
