# HDR colour-matrix test fixtures — how to regenerate

These are **TAG-ONLY fixtures**: they are cut from an existing SDR master (or
stream-copied from the existing HDR master excerpt) and then **re-tagged** via
the `setparams` filter (or, for the master excerpt, stream-copied verbatim).
They are **not** distinct HDR camera footage — the pixel content is ordinary
SDR/SDR-master frames with colour-metadata tags forced on top. Nothing here
tone-maps or changes pixels; only the stream metadata fields move.

Everything is regenerable from `~/source-archive/lovenightwalks/master-2qo-3600-4500.mp4`
(an ffmpeg two-pass libx264 encode already tagged `bt709/bt709/bt709/tv`) plus
the public ShipStream master excerpt URL (for the one PQ excerpt fixture).

## The 3 in-repo fixtures (`tests/fixtures/real/`, committed in ccba723)

Source: the same real SDR master as the staging set. The `setparams` filter is
required because the source's own bt709 primaries/transfer would otherwise be
propagated by libx264 and override the encoder output flags.

```bash
SRC=~/source-archive/lovenightwalks/master-2qo-3600-4500.mp4
cd tests/fixtures/real

# PQ transfer + bt709 primaries/matrix (incoherent → must NOT be HDR)
ffmpeg -hide_banner -loglevel error -y -ss 8 -i "$SRC" -t 1 -map 0:v:0 -an \
  -vf "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p,setparams=color_primaries=bt709:color_trc=smpte2084:colorspace=bt709:range=tv" \
  -c:v libx264 -preset fast -crf 23 -movflags +faststart hdr-pq-bt709-primaries.mp4

# PQ transfer + absent primaries/matrix (partial → must NOT be HDR)
ffmpeg -hide_banner -loglevel error -y -ss 8 -i "$SRC" -t 1 -map 0:v:0 -an \
  -vf "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p,setparams=color_primaries=unknown:color_trc=smpte2084:colorspace=unknown:range=tv" \
  -c:v libx264 -preset fast -crf 23 -movflags +faststart hdr-pq-absent-primaries.mp4

# HLG transfer + absent matrix (partial → must NOT be HDR)
ffmpeg -hide_banner -loglevel error -y -ss 8 -i "$SRC" -t 1 -map 0:v:0 -an \
  -vf "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p,setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=unknown:range=tv" \
  -c:v libx264 -preset fast -crf 23 -movflags +faststart hdr-hlg-absent-matrix.mp4
```

Expected tuples (verify with the ffprobe command below):

| file | color_space | color_transfer | color_primaries | color_range |
|---|---|---|---|---|
| `hdr-pq-bt709-primaries.mp4` | bt709 | smpte2084 | bt709 | tv |
| `hdr-pq-absent-primaries.mp4` | unknown | smpte2084 | unknown | tv |
| `hdr-hlg-absent-matrix.mp4` | unknown | arib-std-b67 | bt2020 | tv |

## The `~/rt-base-wt/hdr-fixtures/` staging set this suite also reads

These live outside the repo (default `HDR_FIXTURES_DIR=~/rt-base-wt/hdr-fixtures`
in `tests/test_hdr_color_matrix.py::_fixture`). Commands as documented in that
directory's own `README.md`:

```bash
SRC=~/source-archive/lovenightwalks/master-2qo-3600-4500.mp4

# plain BT.709 SDR (source bt709 VUI propagates, no setparams needed)
ffmpeg -hide_banner -loglevel error -y -ss 8 -i "$SRC" -t 1 -map 0:v:0 -an \
  -vf "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p" \
  -c:v libx264 -preset fast -crf 23 -movflags +faststart sdr-bt709.mp4

# bt2020 primaries + bt709 transfer (SDR)
ffmpeg -hide_banner -loglevel error -y -ss 8 -i "$SRC" -t 1 -map 0:v:0 -an \
  -vf "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p,setparams=color_primaries=bt2020:color_trc=bt709:colorspace=bt709:range=tv" \
  -c:v libx264 -preset fast -crf 23 -movflags +faststart sdr-bt2020-primaries-bt709-transfer.mp4

# bt2020 primaries + unknown transfer (incomplete metadata)
ffmpeg -hide_banner -loglevel error -y -ss 8 -i "$SRC" -t 1 -map 0:v:0 -an \
  -vf "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p,setparams=color_primaries=bt2020:color_trc=unknown:colorspace=bt709:range=tv" \
  -c:v libx264 -preset fast -crf 23 -movflags +faststart sdr-incomplete-metadata.mp4

# HLG (coherent HDR): bt2020nc / arib-std-b67 / bt2020 / tv
ffmpeg -hide_banner -loglevel error -y -ss 8 -i "$SRC" -t 1 -map 0:v:0 -an \
  -vf "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p,setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc:range=tv" \
  -c:v libx264 -preset fast -crf 23 -movflags +faststart hlg-arib-std-b67.mp4
```

The PQ master excerpt (coherent HDR) is a **stream copy** (no re-encode) from
the public ShipStream master URL, 400–416 s window:

```bash
/opt/homebrew/bin/ffmpeg -hide_banner -y -ss 400 \
  -i "https://shipstream.risingtidesviral.com/assets/vault/lovenightdrives/intake/61ede3fb73262dc98048e31a4e8a96913ea4717d83809a132ed790d45e950fb1.mp4" \
  -t 16 -map 0:v:0 -c copy \
  master-excerpt-400-416.mp4
```

## Verify a fixture's tags

```bash
ffprobe -v error -select_streams v:0 \
  -show_entries stream=color_space,color_transfer,color_primaries,color_range \
  -of default=noprint_wrappers=1 <file>
```

## Which real-file tests skip when the seeno fixture paths are absent

The in-repo fixtures (`tests/fixtures/real/`) are committed and always present,
so the `_repo_fixture` tests only skip when ffmpeg/ffprobe are missing. The four
tests below read the **external** `~/rt-base-wt/hdr-fixtures/` set via
`_fixture(...)` and **skip in CI when those paths are not present**:

- `test_hdr_render_restores_matrix_tag_and_keeps_pixels_identical` (needs `master-excerpt-400-416.mp4`)
- `test_hlg_render_restores_matrix_tag` (needs `hlg-arib-std-b67.mp4`)
- `test_real_probe_path_sdr_inputs_keep_pre_change_argv` (needs `sdr-bt709.mp4`, `sdr-bt2020-primaries-bt709-transfer.mp4`, `sdr-incomplete-metadata.mp4`)
- `test_real_probe_path_sdr_output_is_never_tagged_hdr` (same three SDR fixtures)
