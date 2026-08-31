# Build-input pins

An image rebuilt a year from now must install the *same* package versions, or a campaign
re-run from its records runs different software while reporting the same provenance. Pinning
versions alone does not achieve that: `apt-get install pkg=1.2.3-4` fails once the archive
drops the superseded version, and both `packages.ros.org` and the Ubuntu archive keep only
current ones. What makes an old version *available* is a point-in-time archive.

Everything here was verified against the live services, not assumed. `snapshot-probe.Dockerfile`
is that verification, kept so the recipe can be re-checked in one `docker build` when a step
stops working.

## The two archives, and how they differ

| | endpoint | granularity |
|---|---|---|
| Ubuntu | `https://snapshot.ubuntu.com/ubuntu/<YYYYMMDDTHHMMSSZ>` | any timestamp since 2023-03-01 |
| ROS | `http://snapshots.ros.org/<distro>/<YYYY-MM-DD>/ubuntu` | a **fixed set** of dates, roughly quarterly |

That asymmetry matters for tooling: an Ubuntu pin can be computed from any date, while a ROS pin
has to be **discovered** from what the server actually offers. Asking for an arbitrary ROS date
gets a 404, not the nearest snapshot.

**The two dates are not one point in time**, which was the first thing this got wrong. Each is
bounded by something different — the ROS date by what exists, the Ubuntu stamp by the base image's
own apt state — so they are resolved from different sources and the Ubuntu one is normally *newer*.
Point 7 below is what happens when they are coupled.

## Seven things that only showed up by running it

1. **Install `ca-certificates` from the default archive first.** `snapshot.ubuntu.com` redirects
   HTTP to HTTPS, so apt cannot reach it until the trust store exists — and it cannot install the
   trust store from a source it cannot reach. Switching sources before that step fails with
   "certificate is NOT trusted", which reads like a broken snapshot rather than an ordering bug.
   (`container/robovast/Dockerfile` needs no such step: the ROS base already carries the trust
   store. The gotcha bites a bare `ubuntu` base — which is what the probe uses, and why it has one.)
2. **The ROS snapshots are signed by a different key than the live repo.**
   `AD19BAB3CBF125EA`, *ROS Snapshot builder* `<rosbuild@ros.org>`. The current `ros.key` does not
   verify them: `apt-get update` fails with `NO_PUBKEY AD19BAB3CBF125EA`.
3. **Fetch that key with `curl`, not `gpg --recv-keys`.** The keyserver protocol needs `dirmngr`
   and outbound access a build container may not have; `gpg --recv-keys` exits 2 there. The HTTPS
   keyserver interface plus `gpg --dearmor` needs neither.
4. **`--dearmor` is required.** `ros.key` and keyserver output are ASCII-armored, while a
   `signed-by=…/x.gpg` source expects a binary keyring, and the mismatch reports as `NO_PUBKEY`
   — the same symptom as the wrong key, which makes it easy to misdiagnose.
5. **`grep -R`, not `grep -r`, to find the sources to remove.** The ROS base ships
   `/etc/apt/sources.list.d/ros2.sources` as a **symlink** into `/usr/share/ros-apt-source/`, and
   `-r` skips symlinks it meets while recursing. With `-r` the rolling repo survives, apt keeps
   preferring whichever version is higher, and the snapshot pin silently does nothing — a build
   that looks pinned and is not. This is why the Dockerfile ends the step by asserting on
   `apt-cache policy` rather than trusting the removal: the removal has already been wrong once,
   and the failure mode is invisible. Removing by URL rather than by filename is the other half —
   the name has already moved upstream once (`ros2-latest.list` → `ros2.sources`).
6. **Carry all four Ubuntu suites over.** The base configures `noble`, `-updates`, `-backports`
   (archive.ubuntu.com) and `-security` (security.ubuntu.com) as *two* sources. A snapshot switch
   that writes only `noble noble-updates` silently drops backports and security updates, so the
   rebuilt image is not the same image — and `snapshot.ubuntu.com` serves all four, so there is no
   reason to.
7. **The Ubuntu snapshot must be at or after the base image's own apt state — so it comes from the
   base, not from the ROS date.** `osrf/ros` is rebuilt daily, so its packages are current, while
   the newest ROS snapshot can be months old. Against the older Ubuntu archive `udev` resolves to
   `255.4-1ubuntu8.16` while the base already carries `libudev1 8.17`, so apt is asked to upgrade
   the library without its binary and refuses:

   ```text
   udev : Depends: libudev1 (= 255.4-1ubuntu8.16) but 255.4-1ubuntu8.17 is to be installed
   E: Unable to correct problems, you have held broken packages.
   ```

   Every install pulling `udev` in fails — `xserver-xorg-core`, so the software-rendering step, not
   some corner package. `apt-get upgrade` does **not** rescue it: it unpacks nothing, because the
   snapshot has no newer `libudev1` to move to. The constraint is one-sided, so the fix is one-
   sided too: read the stamp off the base image (`imagetools inspect --format {{.Image.Created}}`,
   no pull) and let the ROS date be whatever the server offers.

## Verified output

```text
ubuntu-snapshot Candidate: 2.1.1-2ubuntu3.24.04.2
ros-snapshot    Candidate: 0.11.0-1noble.20260615.174419
```

The ROS version string carries the snapshot's own build stamp (`20260615.174419`), which is what
makes a pinned rebuild checkable: the version says which snapshot produced it.

## Refreshing the pins

```sh
make refresh-build-pins            # report, then offer to apply it
make refresh-build-pins WRITE=1    # rewrite the base digests without asking
```

The report is printed before the question, because the diff has to be in front of the decision.
With no terminal to ask on it stays a report — a script that inherited the command must not move a
pin because nobody was there to say no.

Re-resolves every digest-pinned `FROM` from the tag kept beside it — which is why the tag is kept
— and prints the newest snapshot date the ROS server actually offers. Deliberately manual and
occasional, the model of `poetry.lock` or `cargo update`: pins that never move rot into an
unbuildable state, and pins that move automatically defeat the point. CI builds only from what is
committed.

The Ubuntu stamp is read from the **base image's** creation timestamp, not derived from the ROS
date — see point 7. That also means the base digest and the Ubuntu pin are refreshed from the same
source and cannot drift apart.

`refresh-build-pins` rewrites `ARG UBUNTU_SNAPSHOT` and `ARG ROS_SNAPSHOT` in
`container/robovast/Dockerfile` together, resolving the ROS date **once** for all files so a refresh
cannot straddle a publication and leave two Dockerfiles at different points in time.

Moving these ARGs changes what a rebuild installs, so it has to change the image cache key too.
That happens through the base image rather than through a snapshot-specific input: a refresh
rebuilds the robovast image, which gives it a new image ID, and `build_hash` hashes that ID rather
than the tag it was asked for (`ImageBuildStore._base_identity`). Hashing the tag was the hole —
`ghcr.io/cps-test-lab/robovast:latest` names different bytes before and after a refresh, so a
campaign image would have been served from cache against the old archives.

## Fetching them from a mirror

The pins say *which* bytes; `ARG UBUNTU_SNAPSHOT_MIRROR` says whose copy of them is downloaded,
for a network with a slow or blocked path to the snapshot service:

```sh
make release-images PROJECT=... UBUNTU_MIRROR=https://<mirror>/ubuntu-snapshot
./container/robovast/build.sh --image all --ubuntu-mirror https://<mirror>/ubuntu-snapshot
```

Three properties make it safe to use on a release:

- **It must mirror the snapshot service**, not a rolling archive. The stamp is appended to the
  URL, so a rolling mirror answers 404 instead of installing today's packages into an image whose
  labels claim a dated archive. `build.sh` rejects a URL that already ends in a stamp, which is
  the other half of the same mistake.
- **Both ROS images use it.** `Dockerfile.roqsim` inherits the base image's sources and repoints
  them for its own installs — the heavier half, so a mirror covering only the base would leave
  most of the download on the slow path.
- **It never reaches the image.** Each Dockerfile restores the canonical URI after its last
  install, because an image carrying a site's mirror ships a host nobody outside that network
  resolves, and `org.robovast.ubuntu-snapshot` would then record an archive that image cannot
  reach. `tests/service/test_build_pins.py` checks the restore, and that the committed default is
  the snapshot service itself.

`build.sh` probes the mirror before building — one request for the dated path — so a mirror of the
rolling archive is refused in a second, with the two ways forward, instead of failing inside apt
four layers in.

## Building without a pin

The mirror most sites have is a mirror of the *rolling* archive, which has no dated paths at all.
That is usable, but only by saying so:

```sh
make release-images PROJECT=... UBUNTU_MIRROR=https://<mirror>/ubuntu UBUNTU_SNAPSHOT=none
```

`none` drops the stamp from the source line, installs whatever that archive serves today, and
records `org.robovast.ubuntu-snapshot=none` on the image. Everything downstream reads that:
`check_recipe.py` reports the recipe as unable to reinstall those versions, and
`rebuild_from_recipe.py` refuses rather than "rebuilding" against an archive that has moved on.
The image ships `http://archive.ubuntu.com/ubuntu`, since there is no dated archive for it to name.

It cannot happen by accident — `none` is spelled out by the caller, it is refused without a
mirror (the snapshot service has nothing to serve without a stamp), and the committed default in
the Dockerfile is always a real stamp. Use it for a dev registry; for a family a published result
depends on, the pin is the whole point.

ROS has no equivalent knob: `snapshots.ros.org` is written into the same step, and nothing has
needed a mirror of it.

## Re-verifying

```sh
docker build -f container/pins/snapshot-probe.Dockerfile \
  --build-arg UBUNTU_SNAPSHOT=<stamp> --build-arg ROS_SNAPSHOT=<date> container/pins
```

Cheap (a small base image, two `apt-get update`s) and it fails loudly on the first four points
above, so it is the thing to run before blaming a rebuild. Points 5 and 6 are properties of the ROS
base rather than of the archives, so they are checked where they live: at build time by the
Dockerfile's own assertion on `apt-cache policy`, and textually by
`tests/service/test_build_pins.py`.

## Not covered here: the source pins

This file is about third-party ground — which base image, which apt archives. *Which commit of our
own code* an image bakes (roqsim, scenario-execution, the scenario-execution-server) is pinned as
`ARG <NAME>_REF` in the Dockerfiles and moved with `make release-images-update-versions`, which
asks the same way (`WRITE=1` skips the question),
which re-resolves each one from its `<NAME>_REPO` with `git ls-remote`. Two commands rather than
one because they are different decisions: refreshing a build pin takes what upstream published,
while moving a source pin changes what the image *does* and belongs in its own reviewable diff.
See `docs/images.rst`, "Which sources an image bakes".
