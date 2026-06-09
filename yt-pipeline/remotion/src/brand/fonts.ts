// Brand font loading. THIS WAS THE BUG: abnTokens declared "TikTok Sans" since day one but no
// loader existed, so every render (captions, lower thirds, title cards, sting) silently fell back
// to Arial — the brand typography only ever existed on the ImageMagick PNG cards. These are the
// SAME TTFs cards.py burns onto cards, so Remotion text and card text now share identical glyphs.
//
// "TikTok Sans Display" = 36pt optical cut (drawn for huge sizes — headlines, captions).
// "TikTok Sans Text"    = 16pt optical cut (mid sizes — badges, sublabels).
// Mono (source labels) falls back to SF Mono/Menlo until a JetBrains Mono TTF is staged.
import { loadFont } from "@remotion/fonts";
import { staticFile } from "remotion";

export const fontsReady = Promise.all([
  loadFont({ family: "TikTok Sans Display", url: staticFile("fonts/TikTokSans36pt-Black.ttf"), weight: "900" }),
  loadFont({ family: "TikTok Sans Display", url: staticFile("fonts/TikTokSans36pt-ExtraBold.ttf"), weight: "800" }),
  loadFont({ family: "TikTok Sans Text", url: staticFile("fonts/TikTokSans16pt-Bold.ttf"), weight: "700" }),
  loadFont({ family: "TikTok Sans Text", url: staticFile("fonts/TikTokSans16pt-SemiBold.ttf"), weight: "600" }),
]).catch((e) => {
  // A missing font file must never kill a render — fall back to the chain in abnTokens.fonts.
  console.error("ABN font load failed (rendering with fallback chain):", e);
});
