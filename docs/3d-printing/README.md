# 3D Printing pack

Swap in model-specific manual when printer model is known.

These docs are general FDM troubleshooting written from Prusa's public knowledge base (help.prusa3d.com), because we don't know the makerspace printer model yet. Menu paths and temperatures are Prusa's. Other brands use different menus. When the model is known, replace or add docs from that printer's official manual, then update the cases in `tests/questions.md`.

This README is not sent to the model. Every other `.md` file in this folder is.

## Staff-only steps

A numbered step that ends with `{staff_only: true}` is never walked through with a visitor. The app hands it to staff instead. This is enforced in code (`app/packs.py`, `app/conversation.py`). Mark any step that involves heating the nozzle or bed manually, cold pulls, opening the printer, or tools. When unsure, mark it.

`staff_only_terms` in `pack.yaml` is a second safeguard. Any model-written text that contains one of these phrases is replaced with a hand-off to staff.

Only numbered lines count as steps. Use bullets for lists of signs or causes.
