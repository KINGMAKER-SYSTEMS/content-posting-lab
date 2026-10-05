# HDR metadata fixtures

The three committed clips are ordinary SDR footage re-encoded with PQ/HLG
metadata using `setparams`. They exercise real probing and known-field
restoration; they are not camera-HDR footage or proof of HDR image quality.
The source is `~/source-archive/lovenightwalks/master-2qo-3600-4500.mp4`.

The output must retain the complete tuple below, including its PQ/HLG transfer.
Missing metadata is not synthesized, and partial or mixed declared HDR logs
`hdr_partial_or_mixed`. This diagnostic does not convert the footage to SDR.

## Regenerate the committed fixtures

The source is tagged `bt709/bt709/bt709/tv`. `setparams` changes the frame
metadata that libx264 propagates; encoding flags alone can be overridden by it.

```bash
SRC=~/source-archive/lovenightwalks/master-2qo-3600-4500.mp4
cd tests/fixtures/real

# PQ transfer + bt709 primaries/matrix (mixed metadata; preserve the known matrix)
ffmpeg -hide_banner -loglevel error -y -ss 8 -i "$SRC" -t 1 -map 0:v:0 -an \
  -vf "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p,setparams=color_primaries=bt709:color_trc=smpte2084:colorspace=bt709:range=tv" \
  -c:v libx264 -preset fast -crf 23 -movflags +faststart hdr-pq-bt709-primaries.mp4

# PQ transfer + absent primaries/matrix (partial metadata; restore only known fields)
ffmpeg -hide_banner -loglevel error -y -ss 8 -i "$SRC" -t 1 -map 0:v:0 -an \
  -vf "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p,setparams=color_primaries=unknown:color_trc=smpte2084:colorspace=unknown:range=tv" \
  -c:v libx264 -preset fast -crf 23 -movflags +faststart hdr-pq-absent-primaries.mp4

# HLG transfer + absent matrix (partial metadata; restore only known fields)
ffmpeg -hide_banner -loglevel error -y -ss 8 -i "$SRC" -t 1 -map 0:v:0 -an \
  -vf "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p,setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=unknown:range=tv" \
  -c:v libx264 -preset fast -crf 23 -movflags +faststart hdr-hlg-absent-matrix.mp4
```

Expected input and rendered output tuples (verify with ffprobe below):

| file | color_space | color_transfer | color_primaries | color_range |
|---|---|---|---|---|
| `hdr-pq-bt709-primaries.mp4` | bt709 | smpte2084 | bt709 | tv |
| `hdr-pq-absent-primaries.mp4` | unknown | smpte2084 | unknown | tv |
| `hdr-hlg-absent-matrix.mp4` | unknown | arib-std-b67 | bt2020 | tv |

## The `~/rt-base-wt/hdr-fixtures/` staging set this suite also reads

These live outside the repo (default `HDR_FIXTURES_DIR=~/rt-base-wt/hdr-fixtures`
in `tests/test_hdr_color_matrix.py::_fixture`). The SDR and HLG fixtures below
are also SDR footage with selected tags; the PQ excerpt is a separate stream
copy. Regeneration commands:

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

# Complete HLG metadata on SDR footage: bt2020nc / arib-std-b67 / bt2020 / tv
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

## Coverage and optional fixtures

The committed fixtures require ffmpeg/ffprobe. The self-contained
`test_synthetic_hdr_signal_preserves_output_tuple_and_pixels` additionally
computes PQ/HLG code values from linear grey levels and encodes them as 10-bit
video with libx264. Complete and missing-primaries tuples must preserve their
actual output fields and decoded pixels. These are synthetic signals, not camera
footage or phone-display validation.

The tests below read the external `HDR_FIXTURES_DIR` set and skip when absent:

- `test_hdr_render_restores_matrix_tag_and_keeps_pixels_identical` (needs `master-excerpt-400-416.mp4`)
- `test_hlg_render_restores_matrix_tag` (needs `hlg-arib-std-b67.mp4`)
- `test_real_probe_path_sdr_inputs_keep_pre_change_argv` (needs `sdr-bt709.mp4`, `sdr-bt2020-primaries-bt709-transfer.mp4`, `sdr-incomplete-metadata.mp4`)
- `test_real_probe_path_sdr_output_is_never_tagged_hdr` (same three SDR fixtures)
