# PNG Mockup

This is an independent Windows PNG/PSD application. Work only inside this
project; never edit, synchronize into, or build the original AutoMockup project.

- Application source of truth: `App Source/`.
- Product identity: `png-mockup`; display name: `PNG Mockup`.
- Keep PNG/PSD naming, Smart Object mapping, image processing and sequential
  execution behavior compatible with the original unless explicitly approved.
- There is no Idea, browser login, prompt, PrintArea or background removal flow.
- Settings, queue, logs, runtime and temporary files belong only to this app.
- Online updates use the product-validated channel `minhtuan5991/PNG-Mockup`.
  Never reuse the original application's update repository. Source checkouts
  must never update themselves over the developer's working tree.
- Preserve user `config.json`, `PNG`, `Mockup`, `Output` and `logs` when installing.
- New installation chooses a parent and creates its `PNG Mockup` child; updates
  target the existing installation exactly. Never replace files while it runs.
- Hide only this app's technical items. Uninstall removes all application,
  configuration, logs, update cache and owned temporary data; it preserves only
  the `PNG`, `Mockup` and `Output` folders with their contents.
- Never bundle GitHub credentials. Check and download in the background; apply
  an update only after explicit UI approval and the current session has closed.
- Build installers under this project and verify extracted payload hashes,
  runtime relocation, product identity and the five UI color presets.
- Never publish releases or sign executables without explicit authorization.
