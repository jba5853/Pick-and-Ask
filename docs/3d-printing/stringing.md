# Stringing and oozing

> Source: https://help.prusa3d.com/article/stringing-and-oozing_1805 · Retrieved 2026-11-11

## Signs

Thin hairs or cobweb-like strands of plastic between separate parts of a print, sometimes called "hairy prints".

## Common causes

- Nozzle temperature too high.
- Wrong retraction settings in the slicer.
- Old residue in the nozzle from earlier filaments.
- Damp filament. Moisture causes heavy stringing.

## How to fix it

1. Clean old residue off the nozzle before printing. {staff_only: true}
2. Use the slicer's factory filament and printer presets. If you changed settings, check the retraction values.
3. Lower the nozzle temperature by 5–10 °C (Filament settings, Nozzle) and see if stringing drops.
4. Check retraction (Printer settings, Extruder): a slightly higher retraction speed helps, and keep "Retract on layer change" and "Wipe while retracting" on. Prusa's maximum retraction length is 2 mm on MK3-style printers.
5. Turn on "Avoid crossing perimeters" so the nozzle travels over the part instead of across gaps.
6. Try a different, dry spool of filament. Store filament dry.
7. To clean strings off a finished print, wave a heat gun at about 200 °C over them for one or two seconds. Don't hold heat on the print longer than that. {staff_only: true}
