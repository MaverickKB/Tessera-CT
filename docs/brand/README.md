# Tessera brand

This is the public subset of the locked artwork. Open the [brand board](board/index.html)
to review the marks, wordmarks, and menu glyphs together. The board uses
local assets and system fonts. It needs no build step or external service.

## Visual lock

- The mark is a binary strata tree. Each parent has exactly two children. The
  nodes are compressed layer-stones. The silhouette reads as a tree at 16 pixels.
- V1 Binary Strata is the lead mark. V2 Faceted Strata and V3 Deep Strata are
  approved alternatives. Preserve their geometry.
- The mark is a tree grown from stones. The glowing node is the warm point in
  the mark. That warm color carries across skins.
- The tree geometry remains the identity. The lettering can evolve.
  Wordmark animation, microinteractions, and banner animation are wanted. Use light
  and color around the supplied geometry, with steady layout and a reduced-motion
  presentation.
- Use the tree on identity surfaces: the app icon, the header, and about or login
  screens. Menu glyphs remain plain line icons with no tree inside them.
- Tessera is the wordmark.
- Keep the palette quiet. Graphite and ink, with a single warm amber. Paper uses
  `#faf9f5` / `#201f1c`, accent `#8a3f2c` / `#edaf97`, stone `#c4b8a8`.

The header uses the supplied branching mark. Its banner decoration carries the
connection between the stone roots, the growing branches, and the warm node.

## Public files

| Location | Contents |
| --- | --- |
| [board/index.html](board/index.html) | Public visual board using the static assets below |
| [icon/mark-cool.svg](icon/mark-cool.svg) | Lead idle mark. `mark.svg` adds the watchman. `mark-mono.svg` is monochrome |
| `icon/sizes/` | PNG exports at 64, 128, 256, 512, and 1024 pixels |
| `wordmark/` | Light and dark lockups, text-only lockups, and the Tessera lockup, in SVG and PNG |
| `glyphs/svg/` | Six plain menu glyphs: chat, work board, infrastructure, voice, integrations, settings |
| `glyphs/png-24/`, `glyphs/png-32/` | Raster glyph exports for light, dark, and monochrome uses |

These SVGs and PNGs are the supplied static exports, without redrawing. The
public board replaces the annotated review sheet. Local source archives,
generator tooling, duplicate exports, and internal review notes are excluded
from this public subset. Runtime UI exports remain in [web/brand](../../web/brand/README.md).

## Icon and logo sets

The in-app header shows the tree art and the Tessera-CT product wordmark.
Icon and logo, under Settings and then Appearance, selects the set on the same
theme control as light, dark, and system. The alternate value is `gremlin`.

The default set is the tree art with the Tessera-CT wordmark and the header
tree. `gremlin` uses `web/brand/gremlin-logo.svg` for the header logo and the
favicon. That sticker is a separate drawing from the F-it button.
`web/brand/f-it-gremlin.svg` remains the only drawing of the button cartoon.
The gremlin set appears only while the F-it button setting is on. There is no
second switch.

The open page updates its header and its icon links, and `/favicon.ico` follows
the same choice. The web app manifest, the macOS dock icon, and the iOS app
icon stay the tree art. This release does not install alternate native icons.

While the gremlin set is showing, the same header trail that draws the tree
shows the crew instead. The tree returns when the default set is selected. The
crew reads only signals the page already shows, and only while the connection
says Connected, which is the same gate that speeds the tree:

- Nothing running: one goblin napping.
- Load is the open session's busy flag, plus that session's `active_agents`,
  plus its `queued` turns, plus swarm jobs whose status is `running`, plus
  board cards whose status is `running`. One goblin is hauling at load 1, two
  at 2 or 3, and three at 4 or more.
- One goblin tumbles when the open session's error string is set, the live
  turn ended with an error, a shown job is `failed` or `blocked`, or a shown
  board card is `failed` or `blocked`. Up to two others keep hauling when the
  load is above zero.
- While the page is not connected, the crew naps and does not use those counts.

Movement is inside `prefers-reduced-motion: no-preference`, the same place as
the tree. The poses stay put when the OS asks for reduced motion. The trail
stays hidden below 1020px, same as the tree. The iOS example does not draw its
own trail. The embedded workspace is this page, so a wide web view gets the
crew and a phone-width view does not. No native iOS crew was added.

Skins remain `strata`, `basalt`, and `tide`. A way to add a custom skin is a
future update and is not part of this release.

The desktop window and the iOS example show this header. The iOS connect screen
and navigation title also show the Tessera-CT wordmark next to the tree art.
Adopters name their own companion. A neutral template is
[examples/companion](../../examples/companion/README.md). Named persona files
are not in this tree.

See the repository [Apache-2.0 license](../../LICENSE).
