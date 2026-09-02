# Third-party components

PNG Mockup includes these components in its offline distribution. Their original
copyright and license notices are retained in the files below.

| Component | Bundled version | Notice location relative to the app |
| --- | --- | --- |
| CPython | 3.12.13, Windows x64 | `runtime/python/LICENSE.txt` |
| Tcl/Tk | 8.6 | `runtime/python/tcl/tk8.6/license.terms` and the Python runtime notices |
| Pillow and bundled image libraries | 12.3.0 | `runtime/python/Lib/site-packages/pillow-12.3.0.dist-info/licenses/LICENSE` |
| oxipng | 10.2.0, Windows x64 | `tools/oxipng/LICENSE` |
| libwebp (`cwebp` and `dwebp`) | 1.6.0, Windows x64 | `tools/libwebp/COPYING` and `tools/libwebp/PATENTS` |

The Pillow notice includes the notices for its bundled image codecs and other
native dependencies. The Python notice includes notices for components shipped
with the Python runtime. Keep these files with redistributed copies of the app.

`runtime/python/BUNDLED_RUNTIME.md` describes the private runtime layout.
`tools/oxipng/README-PNG-MOCKUP.txt` records the bundled oxipng binary hash and
the lossless fallback options used by PNG Mockup.
`tools/libwebp/README-PNG-MOCKUP.txt` records the official archive and bundled
binary hashes for the automatic POD compression workflow.

Adobe Photoshop is an external prerequisite, is not included in this package,
and is not licensed by PNG Mockup. The PNG Mockup application icon is a separate
vector asset created for this app and is not an Adobe mark.
