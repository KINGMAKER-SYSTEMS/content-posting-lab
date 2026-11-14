# Pin the transform-off comparison

The transform-off test compares the whole clip manifest and its output SHA to a recording from actual main commit 60db42476d84095e915fe9b4a698ca950b11fff7. The test uses fixed encoder output in both runs to make the comparison independent of ffmpeg builds; actual encoding is tested elsewhere.

The recording used a detached checkout of that commit and the current test harness and synthetic source fixture. The test-only encoder wrote the fixed bytes `deterministic encoded clip`, and the job ID was fixed. Run `pytest -q tests/test_control_plane_source_execution.py -k test_transforms_all_off_cut_bytes_match_main` to exercise the same job. The fixture file SHA-256 is `a4a401afd85c024c728358156aacf0381ffae240899761d9b8670872aad056d3`.

The encoder call is also recorded from historical main, with only source and destination directory prefixes removed. Its fixture SHA-256 is `489e9b5a03b233643c85166368c1d4fc9d5214df959cb0590d3c80072671cad0`. The test compares every encoder argument, including treatment, cut window and encode options.
