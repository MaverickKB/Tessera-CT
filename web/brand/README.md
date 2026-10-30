# Approved Tessera assets

These are unmodified copies from the supplied Frame board 02 lock pack,
V1 Binary Strata. Use the tree wordmark for identity. Tessera is the product
name. The mark is a binary tree made of compressed strata.

Source mapping from `docs/brand/`:

- `wordmark-cairn-light.png`, `wordmark-cairn-dark.png`: corresponding files in
  `wordmark/`. The transparent PNGs preserve the exact supplied typography.
- `icon-16.png`, `icon-32.png`, `icon-128.png`, `icon-256.png`, `icon-512.png`
  ship with the app. Matching docs exports of these sizes were the same bytes
  and were removed. Other exports remain in `docs/brand/icon/sizes/`.
- `glyphs/*.svg`: corresponding files in `glyphs/svg/`. CSS masks apply the
  current text color to the supplied line geometry without changing its paths.

No stacked-stone pile, leafy tree, spark/orb/glow, compass/path, or mosaic
identity mark. No tree glyphs or tree decoration in menus. Never substitute
older artwork for these locked assets. Keep marks on identity surfaces.

All paths are local to the static asset directory. The manifest only supplies
app identity and icon metadata; it adds no worker, background process, or cache.
The header adds a Tessera-CT wordmark beside the tree art. `goblin/` holds the
goblin avatar set: one folder per avatar with `avatar-64.png` and
`avatar-256.png` (square and transparent), plus `manifest.json` naming each one.
The F-it button mark and the header icon use the avatar chosen in Goblin Mode,
so this set replaced the single gremlin sticker. The header crew is drawn once
in the page, in the same trail as the tree, and is not copied into this
directory. Settings selects `cairn` or `gremlin` for the mark; the goblin set is
shown while the mark is `gremlin` or the F-it setting is on. The manifest icons
stay the tree PNGs. Native dock and home-screen icons are not switched. The iOS
example has no native trail. Custom skins are a future update.
