libwebp bundled with PNG Mockup
================================

Version: 1.6.0
Platform: Windows x64
Upstream: https://chromium.googlesource.com/webm/libwebp/+/refs/tags/v1.6.0
Official download page: https://developers.google.com/speed/webp/docs/precompiled
Official archive: libwebp-1.6.0-windows-x64.zip
Official archive SHA-256:
48886f506b21f62e4661f0f4cbfca19800897c385128e8902542d29a950c93f1
cwebp.exe SHA-256:
6a2f5cb5dce71366353ab1d9caf9c636e039f25703acfce1c148eed346f2f72a
dwebp.exe SHA-256:
17c1488bf84b7834e9aa908bb40afd0be2e55d57567d210e96336c56bb6ae993

PNG Mockup uses cwebp and dwebp only as an offline intermediate codec for its
single automatic POD PNG compression workflow. It restores the original alpha
channel exactly, restores ICC/DPI/rendering chunks, applies a bounded quality
gate, and emits a PNG. No WebP file is presented as the user's final output.

The bundled binaries are unmodified files from Google's official archive.
libwebp is distributed under a BSD-style license with an additional patent
grant. The complete upstream COPYING and PATENTS files are adjacent.
