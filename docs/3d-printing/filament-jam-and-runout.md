# Filament jam, loading failure and runout

> Sources: https://help.prusa3d.com/article/filament-runout-31829-core-one-35829-core-one-l-17829-xl-26829-mk4s-13829-mk4-27829-mk3-9s-21829-mk3-9-28829-mk3-5s-23829-mk3-5-12829-mini_916077, https://help.prusa3d.com/article/loading-timeout-31836-core-one-35836-core-one-l-17836-xl-26836-mk4s-13836-mk4-27836-mk3-9s-21836-mk3-9-28836-mk3-5s-23836-mk3-5-12836-mini_916160, https://help.prusa3d.com/article/filament-not-loading-mk4-s-mk3-9-s_411680 · Retrieved 2026-11-11

## Filament runout

**Screen message:** "Filament runout during print, please insert new one."

The filament sensor saw that the spool ran out and paused the print. This is normal and stops the print from failing.

1. Insert new filament when the printer asks, and follow the on-screen steps. The printer heats up and guides you.
2. Make sure the new filament is fully loaded and coming out of the nozzle before you confirm.
3. Press Continue on the screen to resume.

If the printer says it ran out but filament is still there (a false runout), check the filament sensor is clean and its wiring is connected, and that the filament path is smooth with nothing rubbing.

## Filament won't load, or "Filament loading timed out"

The printer couldn't finish loading in time, usually because of a jam, filament in the wrong place or a sensor problem.

1. Check the filament goes into the extruder properly and isn't snagged on the spool or stuck in a guide tube.
2. Cut the end of the filament at a clean 45° angle so it feeds easily.
3. Unload and reload the filament from the printer's Filament menu.
4. If no plastic comes out of the nozzle when the printer asks whether the color is right, push the filament gently toward the extruder and choose Retry.
5. Check the extruder gears and filament sensor area for debris or ground-up plastic.
6. Make sure the filament sensor is clean. If loading still fails, recalibrate it (on Prusa printers: LCD Menu, Control, Calibrations & Tests, Filament sensor calibration).
7. If the nozzle is blocked, see clogged-nozzle.md.
