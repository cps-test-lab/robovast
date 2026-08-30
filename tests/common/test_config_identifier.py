# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What counts as a config-referenced file.

The config identifier hashes files the config *refers to*, so that changing an input a
campaign reads changes its identity. Candidates are found by treating config strings as
possible relative paths — which means the rule for what is *not* a path has to be
explicit, or a value that happens to resolve drags unrelated content into the hash.
"""

from robovast.common.config_identifier import (collect_paths_from_config,
                                               hash_config_referenced_files)


def test_blank_strings_are_not_path_references(tmp_path):
    """An empty config value must not resolve to the project directory.

    ``os.path.join(vast_dir, "")`` is ``vast_dir`` itself and always exists, so collecting
    an empty string as a referenced path makes the hash walk every file under the project.
    Deliberate empty values exist (an empty launch-package name is how ``ros_launch`` is
    told to take a plain file path), and on a campaign directory that walk is both
    ruinously slow and fatal: it races a run's transient files and raises FileNotFoundError
    for a path that existed moments earlier when it was listed.
    """
    (tmp_path / "somefile.txt").write_text("x", encoding="utf-8")
    for blank in ("", " ", "\t"):
        assert collect_paths_from_config({"pkg": blank}, str(tmp_path)) == set(), (
            f"blank value {blank!r} was treated as a path reference")


def test_real_references_are_still_collected(tmp_path):
    """The blank-string guard must not stop genuine references being found."""
    (tmp_path / "files").mkdir()
    launch = tmp_path / "files" / "sim_launch.py"
    launch.write_text("original", encoding="utf-8")

    config = {"name": [None, "files/sim_launch.py"], "absent": "files/nope.py"}
    assert collect_paths_from_config(config, str(tmp_path)) == {"files/sim_launch.py"}

    # And its *content* is what the identity depends on.
    import yaml
    canonical = yaml.safe_dump(config, sort_keys=True)
    before = hash_config_referenced_files(str(tmp_path), canonical)
    launch.write_text("changed", encoding="utf-8")
    hash_config_referenced_files.cache_clear()   # the impl memoizes on (dir, config)
    assert hash_config_referenced_files(str(tmp_path), canonical) != before


def test_editing_a_file_ref_variation_changes_the_config_identifier(tmp_path):
    """A variation's own source decides which configurations exist, so it is part of them.

    ``hash_variation_entrypoints`` walks and hashes every module of an *entry-point*
    variation's package, but a ``<path>.py:<Class>`` reference matches no entry point and
    contributed only ``sha256(name)`` -- so an edited plugin was indistinguishable from the
    original, and a PROV-O export could not even name the module, since it lists only files
    the campaign recorded carrying. Collecting the source as a run file fixes both at once:
    ``hash_run_files`` reads content.
    """
    from robovast.common.config_identifier import hash_run_files

    plugin = tmp_path / "variations"
    plugin.mkdir()
    source = plugin / "doorway.py"
    source.write_text("GAP = 0.8\n", encoding="utf-8")

    before = hash_run_files(str(tmp_path), ["variations/doorway.py"])
    source.write_text("GAP = 1.6\n", encoding="utf-8")
    after = hash_run_files(str(tmp_path), ["variations/doorway.py"])

    assert before != after, "an edited variation must not keep the campaign's identity"
