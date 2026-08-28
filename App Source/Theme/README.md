# PNG Modern

PNG Mockup uses one native Tkinter/ttk style with five color presets:
Mint, Soft Purple (default), Light, Dark and Brown.

The semantic color tokens live in `src/ui_themes.py`. Native buttons, input
controls, section cards, focus states and generated outline icons share these
tokens. No external skin files, remote fonts or downloaded themes are required.

The three pages are Tạo mockup, Công cụ ảnh and Cài đặt. Pages scroll on smaller
Windows screens; the run log expands when more space is available. Alt+1/2/3
changes pages, F5 refreshes, and Ctrl+Enter starts/resumes from Tạo mockup.
The palette selector previews changes; Lưu cài đặt persists the choice.

Update status and progress appear in Cài đặt. A nonmodal notice appears only
after a newer release is found; check/download work does not lock PNG actions.
The install action stays disabled until processing is idle and the downloaded
installer is verified. A missing dedicated channel is reported quietly in
Settings and never routes to a legacy repository.

`tests/test_ui_themes.py` verifies all five presets and WCAG AA text contrast.
`tests/gui_smoke.py` exercises the real Tk widgets with all windows withdrawn.
`tests/test_gui_updates.py` verifies update isolation and close/install behavior.
