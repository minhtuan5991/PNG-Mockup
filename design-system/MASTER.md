# PNG Mockup design system

## Scope

One desktop visual style (`png_modern`) with five color presets. This is a
production workspace for PNG/PSD batches, not a marketing page. The source of
truth for actual colors is `App Source/src/ui_themes.py`.

## Layout and hierarchy

- Left navigation: Tạo mockup, Công cụ ảnh, Cài đặt.
- Main workflow: PNG inputs, PSD mockups, start/resume, progress and job log.
- One emphasized primary action per workspace; stop, retry and tools remain
  visually secondary. Error state includes text, not color alone.
- Scroll the workspace at smaller desktop heights rather than clipping actions.
- Keep image tools and their file dialogs separate from the automatic PSD run.

## Visual rules

- Native Tk/ttk controls with consistent spacing and flat surfaces.
- Segoe UI / Windows font fallback, readable Vietnamese labels.
- Small code-drawn icons, no emoji or external web font dependencies.
- Clear keyboard focus; buttons and text do not shift on hover.
- Semantic colors: background, surface, text, muted, accent, selected, border,
  success, warning, danger, soft-green utility border and log colors.
- Folder/output actions use bold labels. Queue maintenance and watermark selection
  use a light-green outline while keeping keyboard focus in the active palette.
- Five presets: Mint, Tím nhẹ, Sáng, Tối, Nâu. Default: Tím nhẹ.
- Main text and button labels must pass 4.5:1 contrast; test every palette.

## Boundaries

The UI must not introduce Idea, browser login, prompt, PrintArea or background
removal controls. Keep PNG/PSD processing behavior unchanged. Output size/DPI
settings describe watermark requirements; PSD export retains the template canvas.

New styles may be implemented in a later version after approval. Do not add
style-package loading or duplicate palettes to this first version.
