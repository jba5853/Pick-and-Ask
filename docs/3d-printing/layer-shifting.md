# Layer shifting

> Source: https://help.prusa3d.com/article/layer-shifting_2020 · Retrieved 2026-11-11

## Signs

Part of the print is suddenly offset sideways, like a stack of books pushed out of line. Everything above the shift is moved over.

## Common causes

Something stopped the print head or bed from moving normally along the X or Y axis. The most common causes are wrong belt tension and loose motor pulleys. Obstructions, dry rods or the nozzle hitting a curled-up part can also cause it.

## How to fix it

1. Work out which axis shifted (X or Y). The checks are the same for both.
2. Look for anything blocking the moving parts: cables, the cable bundle, tools or filament in the way.
3. Check belt tension. A loose belt has slack. A correct one sounds like a low bass note when plucked. On Prusa MK3 printers, check LCD Menu, Support, Belt Status.
4. Check the set (grub) screws on each motor pulley are tight, with one screw against the flat side of the motor shaft.
5. Check the rods aren't scratched and the bearings are lubricated.
6. Use normal mode, not stealth mode, so crash detection stays on.
7. For large objects, lower the print speed (LCD Menu, Tune, Speed).
8. If overhangs curl up and get hit by the nozzle, increase part-cooling fan speed.
