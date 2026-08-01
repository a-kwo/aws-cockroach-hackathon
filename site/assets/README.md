# site/assets

Everything in this folder is copied verbatim to `web/assets/` by
`scripts/build_web.py`. Markdown files and `.gitkeep` are skipped.

## The console backdrop

The Admin view draws its own scene — a dotted world map, arcs, dials, a
perspective floor. That is what a fresh clone gets, and it is deliberately
modest: hand-written vector chrome does not reach the haze, bloom and depth of
a painted scene.

Drop an image in here named **`hud-backdrop`** and the build uses it instead,
switching the drawn scene off entirely. Both painting at once reads as two
worlds stacked on each other.

    site/assets/hud-backdrop.webp     ->  web/assets/hud-backdrop.webp

Recognised extensions, in the order the build prefers them:

    .avif  .webp  .png  .jpg  .jpeg

`.avif` and `.webp` are preferred because this is the largest thing the page
loads, and at full-bleed resolution the difference is several megabytes.

### What the image needs to do

- **Full bleed.** It is drawn `cover` and centred, so it is cropped on whichever
  axis the window does not match. Keep anything that must survive away from the
  edges.
- **Dark, and darkest in the middle.** The five agent panels and the core sit
  over the centre. A bright or busy middle costs their legibility, and the scrim
  that protects them is deliberately light so it does not flatten your picture.
- **No text.** The page's own type sits on top; a painted label underneath it
  reads as a rendering error.
- **Roughly 16:9 or wider**, at least 1920px on the long edge.

### After dropping one in

    python scripts/build_web.py

The build prints which backdrop it picked up, or says it is drawing the scene
instead.
