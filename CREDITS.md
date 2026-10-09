# Credits

LeetCoach is original work by yib7 under the [MIT License](LICENSE). It bundles two
third-party front-end libraries, vendored under `static/vendor/` so the app needs no
runtime CDN. Everything else in the repo is original to this project.

## Vendored libraries

| File(s) | Library | Version | License | Used for |
| --- | --- | --- | --- | --- |
| `marked.min.js` | [marked](https://github.com/markedjs/marked) | 12.0.2 | MIT (plus the original Markdown BSD-style notice) | Rendering Claude's markdown answer in the browser |
| `highlight.min.js`, `hljs-python.min.js`, `hljs-cpp.min.js`, `hljs-java.min.js` | [highlight.js](https://github.com/highlightjs/highlight.js) | 11.9.0 | BSD-3-Clause | Syntax highlighting (core + the `python`, `cpp`, `java` grammars) |
| `highlight-github-dark.min.css` | highlight.js `github-dark` theme | 11.9.0 | BSD-3-Clause (ships with highlight.js) | Code-block colours |

The vendored files are unmodified minified builds. Both licenses permit redistribution
on condition that the copyright notices and license texts travel with the code, so the
full texts are reproduced under [License texts](#license-texts) below.

## Original assets

- **Icons.** The `[lc]` mark (`static/favicon.svg`, `docs/media/leetcoach.ico`) and the
  inline SVG interface icons in `templates/index.html` and `static/app.js` are drawn for
  this project. No icon set is bundled.
- **Fonts.** None are bundled. The CSS uses the operating system's own UI and monospace
  font stacks.
- **README media.** `docs/media/screenshot.png` and `docs/media/demo.gif` are captures of
  LeetCoach itself.
- **Sample problems.** Test fixtures, the fake-CLI library seed (`scripts/dev/`) and the
  in-app "Sample: Two Sum" quote a sentence or two of well-known LeetCode problem prompts
  (titles, a one-line task, one sample case) as parser input. No full problem statements
  are included.

## Design lineage

Two patterns were adapted from the author's own sibling projects (not third-party code):

- The throwaway-directory, secret-free, resource-capped sandbox runner in `sandbox.py`
  follows the approach used in STATlee.
- The server-sent-events streaming endpoint in `app.py` mirrors the injectable-runner
  pattern (`run_fn` here) from the Xeno RAG project, translated from FastAPI to Flask.

## License texts

### marked (MIT)

```
Copyright (c) 2018+, MarkedJS (https://github.com/markedjs/)
Copyright (c) 2011-2018, Christopher Jeffrey (https://github.com/chjj/)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

marked's license file also carries the notice for the original Markdown:

```
Copyright © 2004, John Gruber
http://daringfireball.net/
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice,
  this list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name "Markdown" nor the names of its contributors may be used
  to endorse or promote products derived from this software without specific
  prior written permission.

This software is provided by the copyright holders and contributors "as is"
and any express or implied warranties, including, but not limited to, the
implied warranties of merchantability and fitness for a particular purpose are
disclaimed. In no event shall the copyright owner or contributors be liable for
any direct, indirect, incidental, special, exemplary, or consequential damages
(including, but not limited to, procurement of substitute goods or services;
loss of use, data, or profits; or business interruption) however caused and on
any theory of liability, whether in contract, strict liability, or tort
(including negligence or otherwise) arising in any way out of the use of this
software, even if advised of the possibility of such damage.
```

### highlight.js (BSD-3-Clause)

```
BSD 3-Clause License

Copyright (c) 2006, Ivan Sagalaev.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
